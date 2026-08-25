"""Minimal service client used by the static-analysis smoke example."""

import requests


def fetch_inventory(base_url: str, item_id: str) -> dict:
    response = requests.get(f"{base_url}/inventory/{item_id}")
    response.raise_for_status()
    return response.json()
