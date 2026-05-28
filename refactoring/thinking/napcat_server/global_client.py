from .napcat_connection import NapCatClient

napcat_client: NapCatClient | None = None


def get_client() -> NapCatClient | None:
    return napcat_client