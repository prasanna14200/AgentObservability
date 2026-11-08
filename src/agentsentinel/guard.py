"""Decorator for routing payment proposals through the trusted gateway entry point."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import ParamSpec

from agentsentinel.payment_gateway import GatewayResult, TrustedGatewayEntryPoint
from agentsentinel.schema.payment import PaymentIntent

P = ParamSpec("P")


def guard(
    entry_point: TrustedGatewayEntryPoint,
) -> Callable[[Callable[P, PaymentIntent]], Callable[P, GatewayResult]]:
    """Route a proposal function's PaymentIntent through the trusted gateway.

    The proposal function must not perform payment or other external side effects.
    This convenience decorator does not sandbox arbitrary Python code.
    """
    if not isinstance(entry_point, TrustedGatewayEntryPoint):
        raise TypeError("guard requires a trusted gateway entry point")

    def decorate(
        propose: Callable[P, PaymentIntent],
    ) -> Callable[P, GatewayResult]:
        @wraps(propose)
        def guarded(*args: P.args, **kwargs: P.kwargs) -> GatewayResult:
            intent = propose(*args, **kwargs)
            if not isinstance(intent, PaymentIntent):
                raise TypeError("guarded proposal must return a PaymentIntent")
            return entry_point.submit(intent)

        return guarded

    return decorate
