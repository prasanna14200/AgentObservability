# AgentSentinel guarded payment quickstart

First configure the gateway in trusted application startup. `payment_gateway`
below is a `PaymentGateway` built with a `PaymentPolicy`, durable
`SQLitePaymentStore` (used for operations and authorization claims), a protected
provider adapter, and—when enabled—an `ApprovalStore` kill switch sharing the
same SQLite file. The adapter, database, and credentials belong to trusted
application code, never the agent.

```python
store = SQLitePaymentStore(database_path, trusted_issuer_id=APPLICATION_ISSUER)
approvals = ApprovalStore(database_path)
payment_gateway = PaymentGateway(
    PaymentPolicy(store), store, store, provider_adapter, kill_switch=approvals
)
authenticated_context_provider = CurrentSessionContextProvider(authenticated_session)
```

`CurrentSessionContextProvider` is application-specific: its `current_context()`
must derive `AuthenticatedScope` from the authenticated application session,
not from proposal fields. Authorization must be issued by the trusted
application approval flow for the exact operation and intent; merely creating
a `PaymentIntent` does not authorize payment. See
[`issue_authorization.py`](../../scripts/issue_authorization.py) for the local
confirmation example and [`demo_purchase.py`](../../scripts/demo_purchase.py)
for gateway wiring. The latter is a manual Sandbox demo, not an offline
quickstart to run casually.

```python
from agentsentinel import TrustedGatewayEntryPoint, guard
gateway = TrustedGatewayEntryPoint(payment_gateway, authenticated_context_provider)
@guard(gateway)
def purchase(intent): return intent
result = purchase(validated_intent)  # The existing proposal becomes a PaymentIntent.
```

`validated_intent` is the `PaymentIntent` produced from the agent's current
proposal; keep the proposal-building logic, but have it return the intent
instead of performing a payment side effect. `purchase(...)` returns a
`GatewayResult`. The concrete offline guard setup and behavior are exercised in
[`test_guard.py`](../../tests/unit/test_guard.py); its tests use fakes and do
not make provider requests.

The decorated function is only a proposal builder and must not perform payment
or other external side effects. Payment execution must always go through
`TrustedGatewayEntryPoint`; never expose the provider adapter to untrusted agent
code. `guard` is an integration convenience, not a Python sandbox: arbitrary
Python code in the same process must be trusted, and private-object
introspection is not prevented. `REVIEW_REQUIRED` requires the existing human
approval flow, and `UNCERTAIN` must be reconciled rather than retried.
