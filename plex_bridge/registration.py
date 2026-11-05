"""Retry and renew Plex player registrations independently for each room."""
import logging
import threading
import time

LOGGER = logging.getLogger("wiim-focus-control")


class RegistrationMaintainer:
    def __init__(self, cloud, advertisements, *, clock=time.monotonic,
                 refresh_interval=900, retry_initial=5, retry_max=300):
        self.cloud = cloud
        self.advertisements = advertisements
        self.clock = clock
        self.refresh_interval = refresh_interval
        self.retry_initial = retry_initial
        self.retry_max = retry_max
        self.states = {}

    def poll_once(self):
        advertisements = self.advertisements()
        for identifier in list(self.states):
            if identifier not in advertisements or not self.cloud.linked(identifier):
                del self.states[identifier]
        for identifier, item in advertisements.items():
            if not self.cloud.linked(identifier):
                continue
            wanted = (item.title, item.provides)
            state = self.states.setdefault(identifier, {
                "published": None, "due": 0, "retry": self.retry_initial,
                "failed": False,
            })
            now = self.clock()
            if now < state["due"] and (state["failed"] or state["published"] == wanted):
                continue
            try:
                self.cloud.publish_endpoint(identifier, device_name=item.title,
                                            provides=item.provides)
            except Exception:
                # One room must never prevent publication of the remaining rooms.
                LOGGER.exception("could not refresh Plex endpoint registration")
                state["failed"] = True
                state["due"] = self.clock() + state["retry"]
                state["retry"] = min(self.retry_max, state["retry"] * 2)
            else:
                LOGGER.info("", extra={"event": "registration_refreshed"})
                state.update(published=wanted, due=self.clock() + self.refresh_interval,
                             retry=self.retry_initial, failed=False)

    def monitor_forever(self, stop=None):
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                self.poll_once()
            except Exception:
                LOGGER.exception("could not refresh Plex endpoint registration")
            stop.wait(1)
