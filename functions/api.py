# File: resilio-connect-scripts/Resilio Connect API/Python3/shotgrid-status-webhooks-firebase/functions/api.py
from enum import Enum
from functools import wraps
from json import JSONDecodeError
import requests

from errors import ApiConnectionError, ApiUnauthorizedError, ApiError

BASE_API_URL = '/api/v2'


def authorized_api_request(func):
    @wraps(func)
    def wrapper(self, url, *args, **kwargs):
        kwargs['headers'] = {
            'Authorization': 'Token {}'.format(self._token),
            'Content-Type': 'application/json'
        }
        kwargs['verify'] = self._verify

        url = self._base_url + url

        try:
            response = func(self, url, *args, **kwargs)
        except requests.RequestException as e:
            raise ApiConnectionError('Connection to Management Console failed', e)

        if response.status_code >= 400:
            try:
                message = response.json().get('message', '')
            except JSONDecodeError:
                message = response.text

            if response.status_code == 401:
                raise ApiUnauthorizedError(message)

            raise ApiError(message)

        return response
    return wrapper


class ApiBaseCommands:
    def __init__(self, address, token, verify):
        self._token = token
        self._address = address
        self._base_url = address + BASE_API_URL
        self._verify = verify

    # Request methods
    @authorized_api_request
    def _get(self, *args, **kwargs):
        return requests.get(*args, **kwargs)

    @authorized_api_request
    def _post(self, *args, **kwargs):
        return requests.post(*args, **kwargs)

    @authorized_api_request
    def _put(self, *args, **kwargs):
        return requests.put(*args, **kwargs)

    @authorized_api_request
    def _delete(self, *args, **kwargs):
        return requests.delete(*args, **kwargs)

    # Helpers
    def _create(self, *args, **kwargs):
        r = self._post(*args, **kwargs)
        try:
            return r.json()['id']
        except JSONDecodeError as e:
            raise ApiError('Response is not a json: {}. {}'.format(r.text, e))

    def _get_json(self, *args, **kwargs):
        r = self._get(*args, **kwargs)
        try:
            return r.json()
        except JSONDecodeError as e:
            raise ApiError('Response is not a json: {}. {}'.format(r.text, e))

    # Agents
    def _get_agents(self):
        return self._get_json('/agents')

    def _get_agent(self, agent_id):
        return self._get_json('/agents/{}'.format(agent_id))

    def _update_agent(self, agent_id, attrs):
        self._put('/agents/{}'.format(agent_id), json=attrs)

    def _get_agent_config(self):
        return self._get_json('/agents/config')

    def _delete_agent(self, agent_id):
        self._delete('/agents/{}'.format(agent_id))

    # Groups
    def _get_groups(self):
        return self._get_json('/groups')

    def _get_group(self, group_id):
        return self._get_json('/groups/{}'.format(group_id))

    def _create_group(self, attrs):
        return int(self._create('/groups', json=attrs))

    def _update_group(self, group_id, attrs):
        self._put('/groups/{}'.format(group_id), json=attrs)

    def _delete_group(self, group_id):
        self._delete('/groups/{}'.format(group_id))

    # Jobs
    def _get_jobs(self):
        return self._get_json('/jobs')

    def _get_job(self, job_id):
        return self._get_json('/jobs/{}'.format(job_id))

    def _create_job(self, attrs, ignore_errors=False):
        return int(self._create('/jobs', params={'ignore_errors': ignore_errors}, json=attrs))

    def _update_job(self, job_id, attrs):
        self._put('/jobs/{}'.format(job_id), json=attrs)

    def _delete_job(self, job_id):
        self._delete('/jobs/{}'.format(job_id))

    def _get_job_groups(self, job_id):
        return self._get_json('/jobs/{}/groups'.format(job_id))

    # Job Runs
    def _get_job_run(self, job_run_id):
        return self._get_json('/runs/{}'.format(job_run_id))

    def _get_job_runs(self, attrs=None):
        return self._get_json('/runs', params=attrs)

    def _create_job_run(self, attrs):
        return int(self._create('/runs', json=attrs))

    def _stop_job_run(self, job_run_id):
        self._put('/runs/{}/stop'.format(job_run_id))

    def _get_job_run_agent(self, job_run_id, agent_id):
        return self._get_json('/runs/{}/agents/{}'.format(job_run_id, agent_id))

    def _get_job_run_agents(self, job_run_id, attrs=None):
        return self._get_json('/runs/{}/agents'.format(job_run_id), params=attrs)

    def _add_agent_to_job_run(self, job_run_id, attrs):
        self._post('/runs/{}/agents'.format(job_run_id), json=attrs)

    def _stop_run_on_agents(self, job_run_id, attrs):
        self._put('/runs/{}/agents/stop'.format(job_run_id), json=attrs)

    def _restart_agent_in_active_job_run(self, job_run_id, attrs):
        self._put('/runs/{}/agents/restart'.format(job_run_id), json=attrs)
