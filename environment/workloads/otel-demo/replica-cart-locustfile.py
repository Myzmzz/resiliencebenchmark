"""Cart-only Locust profile for a Stage-2 replica of OTel Demo.

Two things the platform needs from this file:

* the statistics row must be named exactly ``/api/cart``. Business health,
  fault effect and recovery are all read from that row
  (``runtime_factory.KubernetesTrafficEvidence``); anything else falls back to
  the global aggregate, which a trimmed system would pollute.
* nothing may reach a service the trimmed profile switched off, or its
  failures would land in that same aggregate.

Both requests go to the cart service and nowhere else. ``POST /api/cart``
adds an item and returns the raw cart, and ``GET /api/cart`` reads a session
that was never written to, so it comes back empty. That matters: the frontend
looks a product up in product-catalog for every item it returns, and
product-catalog needs a database this profile does not run.
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
    def write_then_read_cart(self):
        self.client.post(
            CART_ROUTE,
            json={"item": {"productId": PRODUCT_ID, "quantity": 1}, "userId": str(uuid.uuid4())},
            name=CART_ROUTE,
        )
        self.client.get(CART_ROUTE, params={"sessionId": str(uuid.uuid4())}, name=CART_ROUTE)
