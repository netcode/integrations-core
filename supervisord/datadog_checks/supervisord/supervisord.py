# (C) Datadog, Inc. 2010-present
# All rights reserved
# Licensed under Simplified BSD License (see LICENSE)

import itertools
import re
import socket
import time
from collections import defaultdict

import supervisor.xmlrpc
from six.moves import xmlrpc_client as xmlrpclib

from datadog_checks.base import AgentCheck

DEFAULT_HOST = 'localhost'
DEFAULT_PORT = '9001'
DEFAULT_SOCKET_IP = 'http://127.0.0.1'

DD_STATUS = {
    'STOPPED': AgentCheck.CRITICAL,
    'STARTING': AgentCheck.UNKNOWN,
    'RUNNING': AgentCheck.OK,
    'BACKOFF': AgentCheck.CRITICAL,
    'STOPPING': AgentCheck.CRITICAL,
    'EXITED': AgentCheck.CRITICAL,
    'FATAL': AgentCheck.CRITICAL,
    'UNKNOWN': AgentCheck.UNKNOWN,
}

PROCESS_STATUS = {AgentCheck.CRITICAL: 'down', AgentCheck.OK: 'up', AgentCheck.UNKNOWN: 'unknown'}

SERVER_TAG = 'supervisord_server'

PROCESS_TAG = 'supervisord_process'

FORMAT_TIME = lambda x: time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(x))  # noqa E731

SERVER_SERVICE_CHECK = 'supervisord.can_connect'
PROCESS_SERVICE_CHECK = 'supervisord.process.status'

# Example supervisord versions: http://supervisord.org/changes.html
#   - 4.0.0
#   - 3.0
#   - 3.0b2
#   - 3.0a12
SUPERVISORD_VERSION_PATTERN = re.compile(
    r"""
    (?P<major>0|[1-9]\d*)
    \.
    (?P<minor>0|[1-9]\d*)
    (\.
        (?P<patch>0|[1-9]\d*)
    )?
    (?:(?P<release>
        [a-zA-Z][0-9]*
    ))?
    """,
    re.VERBOSE,
)


class SupervisordCheck(AgentCheck):
    def check(self, instance):
        if instance.get('user'):
            self._log_deprecation('_config_renamed', 'user', 'username')
        if instance.get('pass'):
            self._log_deprecation('_config_renamed', 'pass', 'password')

        server_name = instance.get('name')

        if not server_name or not server_name.strip():
            raise Exception("Supervisor server name not specified in yaml configuration.")

        instance_tags = instance.get('tags', [])
        instance_tags.append('{}:{}'.format(SERVER_TAG, server_name))
        supe = self._connect(instance)
        count_by_status = defaultdict(int)

        # Gather all process information
        try:
            processes = supe.getAllProcessInfo()
        except xmlrpclib.Fault as error:
            raise Exception(
                'An error occurred while reading process information: {} {}'.format(error.faultCode, error.faultString)
            )
        except socket.error:
            host = instance.get('host', DEFAULT_HOST)
            port = instance.get('port', DEFAULT_PORT)
            sock = instance.get('socket')
            if sock is None:
                msg = (
                    'Cannot connect to http://{}:{}. '
                    'Make sure supervisor is running and XML-RPC '
                    'inet interface is enabled.'.format(host, port)
                )
            else:
                msg = (
                    'Cannot connect to {}. Make sure supervisor '
                    'is running and socket is enabled and socket file'
                    ' has the right permissions.'.format(sock)
                )

            self.service_check(SERVER_SERVICE_CHECK, AgentCheck.CRITICAL, tags=instance_tags, message=msg)

            raise Exception(msg)

        except xmlrpclib.ProtocolError as e:
            if e.errcode == 401:  # authorization error
                msg = 'Username or password to {} are incorrect.'.format(server_name)
            else:
                msg = 'An error occurred while connecting to {}: {} {}'.format(server_name, e.errcode, e.errmsg)

            self.service_check(SERVER_SERVICE_CHECK, AgentCheck.CRITICAL, tags=instance_tags, message=msg)
            raise Exception(msg)

        # If we're here, we were able to connect to the server
        self.service_check(SERVER_SERVICE_CHECK, AgentCheck.OK, tags=instance_tags)

        # Filter monitored processes on configuration directives
        proc_regex = instance.get('proc_regex', [])
        if not isinstance(proc_regex, list):
            raise Exception("'proc_regex' should be a list of strings. e.g. %s" % [proc_regex])

        proc_names = instance.get('proc_names', [])
        if not isinstance(proc_names, list):
            raise Exception("'proc_names' should be a list of strings. e.g. %s" % [proc_names])

        # Collect information on each monitored process
        monitored_processes = []

        # monitor all processes if no filters were specified
        if len(proc_regex) == 0 and len(proc_names) == 0:
            monitored_processes = processes

        for pattern, process in itertools.product(proc_regex, processes):
            if re.match(pattern, process['name']) and process not in monitored_processes:
                monitored_processes.append(process)

        for process in processes:
            if process['name'] in proc_names and process not in monitored_processes:
                monitored_processes.append(process)

        # Report service checks and uptime for each process
        for proc in monitored_processes:
            proc_name = proc['name']
            tags = instance_tags + ['{}:{}'.format(PROCESS_TAG, proc_name)]

            # Report Service Check
            status = DD_STATUS[proc['statename']]
            msg = self._build_message(proc) if status is not AgentCheck.OK else None
            count_by_status[status] += 1
            self.service_check(PROCESS_SERVICE_CHECK, status, tags=tags, message=msg)
            # Report Uptime
            uptime = self._extract_uptime(proc)
            self.gauge('supervisord.process.uptime', uptime, tags=tags)

        # Report counts by status
        for status in PROCESS_STATUS:
            self.gauge(
                'supervisord.process.count',
                count_by_status[status],
                tags=instance_tags + ['status:{}'.format(PROCESS_STATUS[status])],
            )

        self._collect_metadata(supe)

    @staticmethod
    def _connect(instance):
        sock = instance.get('socket')
        user = instance.get('user') or instance.get('username')
        password = instance.get('pass') or instance.get('password')
        if sock is not None:
            host = instance.get('host', DEFAULT_SOCKET_IP)
            transport = supervisor.xmlrpc.SupervisorTransport(user, password, sock)
            server = xmlrpclib.ServerProxy(host, transport=transport)
        else:
            host = instance.get('host', DEFAULT_HOST)
            port = instance.get('port', DEFAULT_PORT)
            auth = '{}:{}@'.format(user, password) if user and password else ''
            server = xmlrpclib.Server('http://{}{}:{}/RPC2'.format(auth, host, port))
        return server.supervisor

    @staticmethod
    def _extract_uptime(proc):
        start, now = int(proc['start']), int(proc['now'])
        status = proc['statename']
        active_state = status in ['BACKOFF', 'RUNNING', 'STOPPING']
        return now - start if active_state else 0

    @staticmethod
    def _build_message(proc):
        start, stop, now = int(proc['start']), int(proc['stop']), int(proc['now'])
        proc['now_str'] = FORMAT_TIME(now)
        proc['start_str'] = FORMAT_TIME(start)
        proc['stop_str'] = '' if stop == 0 else FORMAT_TIME(stop)

        return (
            """Current time: %(now_str)s
Process name: %(name)s
Process group: %(group)s
Description: %(description)s
Error log file: %(stderr_logfile)s
Stdout log file: %(stdout_logfile)s
Log file: %(logfile)s
State: %(statename)s
Start time: %(start_str)s
Stop time: %(stop_str)s
Exit Status: %(exitstatus)s"""
            % proc
        )

    def _collect_metadata(self, supe):
        try:
            version = supe.getSupervisorVersion()
        except Exception as e:
            self.log.warning("Error collecting version: %s", e)
            return

        self.log.debug('Version collected: %s', version)
        self.set_metadata('version', version, scheme='regex', pattern=SUPERVISORD_VERSION_PATTERN)
