# File: resilio-connect-scripts/Resilio Connect API/Python3/shotgrid-status-webhooks-firebase/functions/errors.py
class ApiError(Exception):
    pass


class ApiConnectionError(ApiError):
    pass


class ApiUnauthorizedError(ApiError):
    pass
