"""The remediation invariant, across every error code."""
import inspect

import pytest

from cirro_agent import errors

CODES = [
    obj for _, obj in vars(errors).items()
    if inspect.isclass(obj) and issubclass(obj, errors.AgentError)
    and obj is not errors.AgentError
]


def test_every_code_is_distinct():
    codes = [cls.code for cls in CODES]
    assert len(codes) == len(set(codes)), codes


@pytest.mark.parametrize("cls", CODES, ids=lambda c: c.code)
def test_an_error_cannot_be_raised_without_a_way_forward(cls):
    with pytest.raises(AssertionError, match="no remediation"):
        cls("boom", what="something went wrong")


@pytest.mark.parametrize("cls", CODES, ids=lambda c: c.code)
def test_retry_with_alone_is_sufficient(cls):
    exc = cls("boom", what="fix it", retry_with=[{"verb": "query"}])
    payload = exc.to_dict()
    assert payload["ok"] is False
    assert payload["error"]["code"] == cls.code
    remediation = payload["error"]["remediation"]
    assert remediation["retry_with"] or remediation["user_action"]


@pytest.mark.parametrize("cls", CODES, ids=lambda c: c.code)
def test_user_action_alone_is_sufficient(cls):
    exc = cls("boom", what="fix it", user_action="do the thing")
    assert exc.to_dict()["error"]["remediation"]["user_action"] == "do the thing"


@pytest.mark.parametrize("cls", CODES, ids=lambda c: c.code)
def test_severity_is_one_of_the_declared_values(cls):
    assert cls.severity in (errors.AGENT_ACTION, errors.USER_ACTION,
                            errors.RETRYABLE, errors.FATAL)


def test_cost_is_reported_even_on_failure():
    exc = errors.CostGate("too much", what="narrow it", retry_with=[{"a": 1}])
    payload = exc.to_dict(cost={"api_calls": 14})
    assert payload["cost"]["api_calls"] == 14, \
        "a failure that burned API calls must say so"


def test_extra_detail_is_preserved():
    exc = errors.NotFound("nope", what="pick another", retry_with=[{}],
                          visible_projects=3)
    assert exc.to_dict()["error"]["detail"]["visible_projects"] == 3
