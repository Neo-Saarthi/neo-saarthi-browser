from dataclasses import dataclass


@dataclass(frozen=True)
class LocalDistributor:
    distributor_id: str = "local-browser-owner"


def get_current_distributor() -> LocalDistributor:
    return LocalDistributor()


def _dev_auth_bypass_enabled() -> bool:
    return False


def _dev_distributor() -> LocalDistributor:
    return LocalDistributor()
