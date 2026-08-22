"""Deterministic Sock Shop entry workload."""

from __future__ import annotations

import base64
import itertools
import json
import os
from urllib import request as urllib_request

from locust import HttpUser, constant_pacing, events, task

from deterministic import (
    deterministic_choice,
    deterministic_uuid,
    exact_percent_schedule,
    install_locust_evaluation_window,
)


SEED = int(os.environ.get("RESBENCH_RANDOM_SEED", "2026082202"))
MAX_CART_ITEM_PRICE = float(os.environ.get("RESBENCH_SOCK_SHOP_MAX_CART_ITEM_PRICE", "15"))
SCHEDULE = exact_percent_schedule(
    SEED,
    (("browse-catalogue", 50), ("view-cart", 20), ("add-to-cart", 15), ("checkout-order", 15)),
)
USER_COUNTER = itertools.count()
MEASUREMENT_WINDOW = install_locust_evaluation_window(events)


class SockShopUser(HttpUser):
    wait_time = constant_pacing(float(os.environ.get("RESBENCH_FLOW_PERIOD_SECONDS", "1")))

    def on_start(self):
        self.user_index = next(USER_COUNTER)
        self.iteration = 0
        self.products: list[str] = []
        self.cart_products: list[str] = []
        self.customer_ref = deterministic_uuid(SEED, self.user_index, 0, "customer")
        username = os.environ.get("SOCK_SHOP_USERNAME", "")
        password = os.environ.get("SOCK_SHOP_PASSWORD", "")
        self.authenticated = False
        if username and password:
            encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
            with self.client.get(
                "/login",
                headers={"Authorization": f"Basic {encoded}"},
                name="/login",
                catch_response=True,
            ) as response:
                if response.status_code == 200:
                    self.authenticated = True
                else:
                    response.failure("benchmark user login failed")
        with self.client.get("/catalogue", name="/catalogue", catch_response=True) as response:
            try:
                items = response.json()
                self.products = sorted(str(item["id"]) for item in items if isinstance(item, dict) and item.get("id"))
                self.cart_products = sorted(
                    str(item["id"])
                    for item in items
                    if isinstance(item, dict)
                    and item.get("id")
                    and isinstance(item.get("price"), (int, float))
                    and float(item["price"]) <= MAX_CART_ITEM_PRICE
                )
            except Exception:
                response.failure("catalogue response is not a product list")
            if not self.products:
                response.failure("catalogue contained no product ids")
            if not self.cart_products:
                response.failure("catalogue contained no checkout-safe product ids")

    def _product(self, label: str) -> str:
        return deterministic_choice(self.products, SEED, self.user_index, self.iteration, label)

    def _cart_query(self) -> str:
        return "" if self.authenticated else "?custId=" + self.customer_ref

    def _add_to_cart(self) -> bool:
        if not self.cart_products:
            return False
        product = deterministic_choice(
            self.cart_products,
            SEED,
            self.user_index,
            self.iteration,
            "cart-product",
        )
        with self.client.post(
            "/cart" + self._cart_query(),
            json={"id": product},
            name="/cart [add]",
            catch_response=True,
        ) as response:
            if response.status_code != 201:
                response.failure("add-to-cart did not return 201")
                return False
        return True

    def _cleanup_order(self, response) -> None:
        try:
            body = response.json()
            order_id = body.get("id")
            if not order_id and isinstance(body.get("_links"), dict):
                href = ((body["_links"].get("self") or {}).get("href") or "").rstrip("/")
                order_id = href.rsplit("/", 1)[-1] if href else ""
            if not order_id:
                raise ValueError("order id missing")
            req = urllib_request.Request(f"http://orders/orders/{order_id}", method="DELETE")
            with urllib_request.urlopen(req, timeout=10) as cleanup_response:
                if cleanup_response.status not in {200, 202, 204}:
                    raise ValueError("order cleanup status was not successful")
        except Exception:
            response.failure("created order could not be cleaned up")

    @task
    def deterministic_flow(self):
        flow = SCHEDULE[self.iteration % len(SCHEDULE)]
        if flow == "browse-catalogue":
            product = self._product("browse-product") if self.products else ""
            path = "/catalogue/" + product if product else "/catalogue"
            self.client.get(path, name="/catalogue [browse]")
        elif flow == "view-cart":
            self.client.get("/cart" + self._cart_query(), name="/cart [view]")
        elif flow == "add-to-cart":
            self._add_to_cart()
        elif flow == "checkout-order":
            if not self.authenticated:
                with self.client.post("/orders", json={}, name="/orders [checkout]", catch_response=True) as response:
                    response.failure("checkout requires the runtime benchmark user")
            else:
                # The payment service intentionally rejects carts over 100.00.
                # All virtual users share the benchmark account, so reset its
                # cart before the order flow and use only bounded-price items.
                # This keeps the workload deterministic without weakening the
                # checkout assertion or changing the declared flow mix.
                with self.client.delete(
                    "/cart" + self._cart_query(),
                    name="/cart [checkout prepare]",
                    catch_response=True,
                ) as response:
                    if response.status_code not in {200, 202, 204}:
                        response.failure("checkout cart reset did not succeed")
                        self.iteration += 1
                        return
                if self._add_to_cart():
                    with self.client.post("/orders", json={}, name="/orders [checkout]", catch_response=True) as response:
                        if response.status_code not in {200, 201}:
                            response.failure("checkout did not succeed")
                        else:
                            self._cleanup_order(response)
        self.iteration += 1

    def on_stop(self):
        self.client.delete("/cart" + self._cart_query(), name="/cart [cleanup]")
