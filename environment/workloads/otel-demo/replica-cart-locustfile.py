"""Cart-only Locust profile for a Stage-2 replica of OTel Demo.

Two things the platform needs from this file:

* the statistics row must be named exactly ``/api/cart``.  Business health,
  fault effect and recovery are all read from that row
  (``runtime_factory.KubernetesTrafficEvidence``); anything else falls back to
  the global aggregate, which a trimmed system would pollute.
* nothing may call a service the trimmed profile switched off, or its failures
  would land in the same aggregate.

Each iteration creates a fresh session, adds one item and reads that cart
back, so the cart stays a single item and the read path keeps its product
lookup without growing over time.
"""

import os
import uuid

from locust import HttpUser, constant_pacing, task

CART_ROUTE = "/api/cart"
PRODUCT_ID = os.environ.get("RESBENCH_CART_PRODUCT_ID", "OLJCESPC7Z")
PACING_SECONDS = float(os.environ.get("RESBENCH_CART_PACING_SECONDS", "1"))


class CartUser(HttpUser):
    wait_time = constant_pacing(PACING_SECONDS)

    @task
    def add_then_view_cart(self):
        session_id = str(uuid.uuid4())
        self.client.post(
            CART_ROUTE,
            json={"item": {"productId": PRODUCT_ID, "quantity": 1}, "userId": session_id},
            name=CART_ROUTE,
        )
        self.client.get(CART_ROUTE, params={"sessionId": session_id}, name=CART_ROUTE)
