# (C) Datadog, Inc. 2021-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
import pytest

from datadog_checks.dev.utils import get_metadata_metrics

from .common import AGENT_V2_METRICS, ON_CI, OPERATOR_V2_METRICS, requires_new_environment

pytestmark = [pytest.mark.skipif(ON_CI, reason="This test environment flakes on CI"), requires_new_environment]


def test_agent_check(aggregator, agent_instance_use_openmetrics, mock_agent_data, dd_run_check, check):
    c = check(agent_instance_use_openmetrics(True))
    dd_run_check(c)
    for m in AGENT_V2_METRICS:
        aggregator.assert_metric(m)
    aggregator.assert_all_metrics_covered()
    aggregator.assert_metrics_using_metadata(get_metadata_metrics())


def test_operator_check(aggregator, operator_instance_use_openmetrics, mock_operator_data, dd_run_check, check):
    c = check(operator_instance_use_openmetrics(True))

    dd_run_check(c)
    for m in OPERATOR_V2_METRICS:
        aggregator.assert_metric(m)
    aggregator.assert_all_metrics_covered()
    aggregator.assert_metrics_using_metadata(get_metadata_metrics())
