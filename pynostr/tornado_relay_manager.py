import json
import logging
import time
from dataclasses import dataclass

from tornado import gen
from tornado.ioloop import IOLoop

from .base_relay import RelayPolicy
from .event import Event
from .exception import RelayException
from .filters import FiltersList
from .message_pool import MessagePool
from .tornado_relay import TornadoRelay
from .utils import get_relay_information

log = logging.getLogger(__name__)


@dataclass
class TornadoRelayManager:
    error_threshold: int = 3

    def __post_init__(self):
        self.relays: dict[str, TornadoRelay] = {}
        self.message_pool: MessagePool = MessagePool()
        self.io_loop: IOLoop = IOLoop.current()

    def add_relay(
        self, url: str, policy: RelayPolicy = RelayPolicy(), close_on_eose: bool = True
    ):

        relay = TornadoRelay(
            url, self.message_pool, self.io_loop, policy, close_on_eose=close_on_eose
        )
        if self.error_threshold:
            relay.error_threshold = self.error_threshold

        self.relays[url] = relay

    def remove_relay(self, url: str):
        if url in self.relays:
            relay = self.relays.pop(url)
            relay.close()

    def remove_closed_relays(self):
        for url, connected in self.connection_statuses.items():
            if not connected:
                log.info(f'{url} is not connected... removing relay.')
                self.remove_relay(url=url)

    def add_subscription_on_relay(self, url: str, id: str, filters: FiltersList):
        if url in self.relays:
            relay = self.relays[url]
            if not relay.policy.should_read:
                raise RelayException(
                    f"Could not send request: {url} " f"is not configured to read from"
                )
            relay.add_subscription(id, filters)

    @gen.coroutine
    def prepare_relays(self, timeout: int = 2):
        futures = []
        relays = []
        for relay in self.relays.values():
            if relay.policy.should_read:
                # yield relay.connect()
                relays.append(relay)
                if timeout > 0:
                    future = gen.with_timeout(
                        self.io_loop.time() + timeout, relay.connect(timeout=0)
                    )
                    futures.append(future)
                else:
                    futures.append(relay.connect(timeout=0))

        if timeout > 0:
            for i, future in enumerate(futures):
                try:
                    yield future
                except gen.TimeoutError:
                    log.warning(
                        f"Connection to WebSocket client {relays[i].url} timed out"
                    )
        else:
            yield gen.multi(futures)
        raise gen.Return(relays)

    def add_subscription_on_all_relays(self, id: str, filters: FiltersList):
        for relay in self.relays.values():
            if relay.policy.should_read:
                relay.add_subscription(id, filters)

    def run_sync(self, timeout: int = 2):
        self.io_loop.run_sync(lambda: self.prepare_relays(timeout))

    def close_subscription_on_relay(self, url: str, id: str):
        if url in self.relays:
            relay = self.relays[url]
            relay.close_subscription(id)
            relay.publish(json.dumps(["CLOSE", id]))
        else:
            raise RelayException(f"Invalid relay url: no connection to {url}")

    def close_subscription_on_all_relays(self, id: str):
        for relay in self.relays.values():
            relay.close_subscription(id)
            relay.publish(json.dumps(["CLOSE", id]))

    def close_all_relay_connections(self):
        for url in self.relays:
            relay = self.relays[url]
            relay.close()

    def open_connections(self, ssl_options: dict = None):
        for relay in self.relays.values():
            if not relay.is_connected:
                self.io_loop.add_callback(relay.start)
                self.io_loop.start()
        time.sleep(2)
        self.remove_closed_relays()

    def close_connections(self):
        for relay in self.relays.values():
            relay.close()

        return any(self.connection_statuses.values())

    @property
    def connection_statuses(self) -> dict:
        """gets the url and connection statuses of relays
        Returns:
            dict: bool of connection statuses
        """
        statuses = [relay.is_connected for relay in self.relays.values()]
        return dict(zip(self.relays.keys(), statuses))

    def publish_message(self, message: str):
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

    def get_relay_information(self):
        ret = {}
        for url in self.relays:
            ret[url] = get_relay_information(url)
        return ret