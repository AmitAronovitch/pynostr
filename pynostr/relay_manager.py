"""Forked from https://github.com/jeffthibault/python-nostr.git."""
import json
import threading
import time
from dataclasses import dataclass
from threading import Lock

from .base_relay import RelayPolicy, RelayProxyConnectionConfig
from .event import Event
from .filters import FiltersList
from .message_pool import MessagePool
from .relay import Relay


class RelayException(Exception):
    pass


@dataclass
class RelayManager:
    error_threshold: int = 0

    def __post_init__(self):
        self.relays: dict[str, Relay] = {}
        self.message_pool: MessagePool = MessagePool()
        self.lock: Lock = Lock()

    def add_relay(
        self,
        url: str,
        policy: RelayPolicy = RelayPolicy(),
        ssl_options: dict = None,
        proxy_config: RelayProxyConnectionConfig = None,
    ):

        relay = Relay(url, self.message_pool, policy, ssl_options, proxy_config)
        if self.error_threshold:
            relay.error_threshold = self.error_threshold

        with self.lock:
            self.relays[url] = relay

        threading.Thread(target=relay.connect, name=f"{relay.url}-thread").start()

        time.sleep(1)

    def remove_relay(self, url: str):
        with self.lock:
            if url in self.relays:
                relay = self.relays.pop(url)
                relay.close()

    def remove_closed_relays(self):
        for url, connected in self.connection_statuses.items():
            if not connected:
                # warnings.warn(f'{url} is not connected... removing relay.')
                self.remove_relay(url=url)

    def add_subscription_on_relay(self, url: str, id: str, filters: FiltersList):
        with self.lock:
            if url in self.relays:
                relay = self.relays[url]
                if not relay.policy.should_read:
                    raise RelayException(
                        f"Could not send request: {url} "
                        f"is not configured to read from"
                    )
                relay.add_subscription(id, filters)
                relay.publish(relay.request)
            else:
                raise RelayException(f"Invalid relay url: no connection to {url}")

    def add_subscription_on_all_relays(self, id: str, filters: FiltersList):
        with self.lock:
            for relay in self.relays.values():
                if relay.policy.should_read:
                    relay.add_subscription(id, filters)
                    relay.publish(relay.request)

    def close_subscription_on_relay(self, url: str, id: str):
        with self.lock:
            if url in self.relays:
                relay = self.relays[url]
                relay.close_subscription(id)
                relay.publish(json.dumps(["CLOSE", id]))
            else:
                raise RelayException(f"Invalid relay url: no connection to {url}")

    def close_subscription_on_all_relays(self, id: str):
        with self.lock:
            for relay in self.relays.values():
                relay.close_subscription(id)
                relay.publish(json.dumps(["CLOSE", id]))

    def close_all_relay_connections(self):
        with self.lock:
            for url in self.relays:
                relay = self.relays[url]
                relay.close()

    def open_connections(self, ssl_options: dict = None):
        for relay in self.relays.values():
            if not relay.is_connected:
                threading.Thread(
                    target=relay.connect,
                    name=f"{relay.url}-thread",
                ).start()
        time.sleep(2)
        self.remove_closed_relays()
        assert all(self.connection_statuses.values())
        self._is_connected = True

    def close_connections(self):
        for relay in self.relays.values():
            relay.close()

        assert not any(self.connection_statuses.values())
        self._is_connected = False

    @property
    def connection_statuses(self) -> dict:
        """gets the url and connection statuses of relays
        Returns:
            dict: bool of connection statuses
        """
        statuses = [relay.is_connected for relay in self.relays.values()]
        return dict(zip(self.relays.keys(), statuses))

    def publish_message(self, message: str):
        with self.lock:
            for relay in self.relays.values():
                if relay.policy.should_write:
                    relay.publish(message)

    def publish_event(self, event: Event):
        """Verifies that the Event is publishable before submitting it to relays."""
        if event.sig is None:
            raise RelayException(f"Could not publish {event.id}: must be signed")

        if not event.verify():
            raise RelayException(
                f"Could not publish {event.id}: failed to verify signature {event.sig}"
            )

        self.publish_message(event.to_message())
