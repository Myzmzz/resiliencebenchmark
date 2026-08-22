"""Deterministic OTel Demo entry workload."""

from __future__ import annotations

import itertools
import os

from locust import HttpUser, constant_pacing, events, task

from deterministic import deterministic_choice, deterministic_uuid, exact_percent_schedule, install_locust_evaluation_window


SEED = int(os.environ.get("RESBENCH_RANDOM_SEED", "2026082203"))
SCHEDULE = exact_percent_schedule(SEED, (("browse-products", 74), ("cart-operations", 19), ("checkout", 7)))
USER_COUNTER = itertools.count()
MEASUREMENT_WINDOW = install_locust_evaluation_window(events)
PRODUCTS = (
    "0PUK6V6EV0",
    "1YMWWN1N4O",
    "2ZYFJ3GM2N",
    "66VCHSJNUP",
    "6E92ZMYYFZ",
    "9SIQT8TOJO",
    "L9ECAV7KIM",
    "LS4PSXUNUM",
    "OLJCESPC7Z",
    "HQTGWGPNH4",
)
CHECKOUT_PERSON = {
    "email": "resbench@example.invalid",
    "address": {
        "streetAddress": "1600 Amphitheatre Parkway",
        "zipCode": "94043",
        "city": "Mountain View",
        "state": "CA",
        "country": "United States",
    },
    "userCurrency": "USD",
    "creditCard": {
        "creditCardNumber": "4432-8015-6152-0454",
        "creditCardExpirationMonth": 1,
        "creditCardExpirationYear": 2039,
        "creditCardCvv": 672,
    },
}


class OtelDemoUser(HttpUser):
    wait_time = constant_pacing(float(os.environ.get("RESBENCH_FLOW_PERIOD_SECONDS", "1")))

    def on_start(self):
        self.user_index = next(USER_COUNTER)
        self.iteration = 0

    def _product(self, label: str) -> str:
        return deterministic_choice(PRODUCTS, SEED, self.user_index, self.iteration, label)

    def _user_id(self, label: str) -> str:
        return deterministic_uuid(SEED, self.user_index, self.iteration, label)

    def _add_to_cart(self, user_id: str) -> None:
        product = self._product("cart-product")
        self.client.get("/api/products/" + product, name="/api/products/:id")
        self.client.post(
            "/api/cart",
            json={"item": {"productId": product, "quantity": 1}, "userId": user_id},
            name="/api/cart [add]",
        )

    @task
    def deterministic_flow(self):
        flow = SCHEDULE[self.iteration % len(SCHEDULE)]
        if flow == "browse-products":
            product = self._product("browse-product")
            selector = self.iteration % 4
            if selector == 0:
                self.client.get("/", name="/")
            elif selector == 1:
                self.client.get("/api/products/" + product, name="/api/products/:id")
            elif selector == 2:
                self.client.get("/api/recommendations", params={"productIds": [product]}, name="/api/recommendations")
            else:
                self.client.get("/api/product-reviews/" + product, name="/api/product-reviews/:id")
        elif flow == "cart-operations":
            user_id = self._user_id("cart-user")
            self._add_to_cart(user_id)
            self.client.get("/api/cart", params={"userId": user_id}, name="/api/cart [view]")
        elif flow == "checkout":
            user_id = self._user_id("checkout-user")
            self._add_to_cart(user_id)
            person = dict(CHECKOUT_PERSON)
            person["userId"] = user_id
            self.client.post("/api/checkout", json=person, name="/api/checkout")
        self.iteration += 1
