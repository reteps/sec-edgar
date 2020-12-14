"""Client to communicate with EDGAR database."""
import asyncio
import os
import time

import requests
from aiohttp import ClientSession, TCPConnector
from bs4 import BeautifulSoup
from secedgar.client._base import AbstractClient
from secedgar.client.async_session import RateLimitedClientSession
from secedgar.utils import make_path
from secedgar.utils.exceptions import EDGARQueryError


class NetworkClient(AbstractClient):
    """Class in charge of sending and handling requests to EDGAR database.

    Attributes:
        retry_count (int): Number of times to retry connecting to URL if not successful.
        pause (float): Time (in seconds) to wait before retrying if not successful.
        batch_size (int): Number of filings to receive per request (helpful if pagination needed).
    """

    _BASE = "http://www.sec.gov/"

    def __init__(self, **kwargs):
        self.retry_count = kwargs.get("retry_count", 3)
        self.pause = kwargs.get("pause", 0.5)
        self.batch_size = kwargs.get("batch_size", 10)
        self.response = None

    @property
    def retry_count(self):
        """int: Number of times to retry request."""
        return self._retry_count

    @retry_count.setter
    def retry_count(self, value):
        if not isinstance(value, int):
            raise TypeError("Retry count must be int. Given type {0}.".format(type(value)))
        elif value < 0:
            raise ValueError("Retry count must be greater than 0. Given {0}.".format(value))
        self._retry_count = value

    @property
    def pause(self):
        """Amount of time to pause between each unsuccessful request before making another."""
        return self._pause

    @pause.setter
    def pause(self, value):
        if not isinstance(value, (int, float)):
            raise TypeError("Pause must be int or float. Given type {0}.".format(type(value)))
        elif value < 0:
            raise ValueError("Pause must be greater than or equal to 0. Given {0}.".format(value))
        self._pause = value

    @property
    def batch_size(self):
        """The Number of results to show per page."""
        return self._batch_size

    @batch_size.setter
    def batch_size(self, value):
        if not isinstance(value, int):
            raise TypeError("Batch size must be int. Given type {0}".format(type(value)))
        elif value < 1:
            raise ValueError("Batch size must be positive integer.")
        self._batch_size = value

    @staticmethod
    def _prepare_query(path):
        """Prepare the query url.

        Args:
            url (str): End of url.

        Returns:
            url (str): A formatted url.
        """
        return "%s%s" % (NetworkClient._BASE, path)

    @staticmethod
    def _validate_response(response):
        """Ensure response from EDGAR is valid.

        Args:
            response (requests.response): A requests.response object.

        Raises:
            EDGARQueryError: If response contains EDGAR error message.
        """
        error_messages = ("The value you submitted is not valid",
                          "No matching Ticker Symbol.",
                          "No matching CIK.",
                          "No matching companies.")
        status_code = response.status_code
        if 400 <= status_code < 500:
            if status_code == 400:
                raise EDGARQueryError("The query could not be completed. "
                                      "The page does not exist.")
            elif status_code == 429:
                raise EDGARQueryError("Error: You have hit the rate limit. "
                                      "SEC has banned your IP for 10 minutes. "
                                      "Please wait 10 minutes "
                                      "before making another request."
                                      "https://www.sec.gov/privacy.htm#security")
            else:
                raise EDGARQueryError("The query could not be completed. "
                                      "There was a client-side error with your "
                                      "request.")
        elif 500 <= status_code < 600:
            raise EDGARQueryError("The query could not be completed. "
                                  "There was a server-side error with "
                                  "your request.")
        elif any(error_message in response.text for error_message in error_messages):
            raise EDGARQueryError()
        # Need to check for error messages before checking for 200 status code
        elif status_code != 200:
            raise EDGARQueryError()

    def get_response(self, path, params, **kwargs):
        """Execute HTTP request and returns response if valid.

        Args:
            path (str): A properly-formatted path
            params (dict): Dictionary of parameters to pass
            to request.

        Returns:
            response (requests.Response): A `requests.Response` object.

        Raises:
            EDGARQueryError: If problems arise when making query.
        """
        prepared_url = self._prepare_query(path)
        response = None
        for i in range(self.retry_count + 1):
            response = requests.get(prepared_url, params=params, **kwargs)
            try:
                self._validate_response(response)
            except EDGARQueryError as e:
                # Raise query error if on last retry
                if i == self.retry_count:
                    raise e
            finally:
                time.sleep(self.pause)
        self.response = response
        return self.response

    def get_soup(self, path, params, **kwargs):
        """Return BeautifulSoup object from response text. Uses lxml parser.

        Args:
            path (str): A properly-formatted path
            params (dict): Dictionary of parameters to pass
            to request.

        Returns:
            BeautifulSoup object from response text.
        """
        return BeautifulSoup(self.get_response(path, params, **kwargs).text, features='lxml')

    async def wait_for_download_async(self, inputs):
        """Asynchronously download links into files using rate limit.

        inputs (list of tuples of str): List of tuples with length 2. First element
            in tuple should be URL to request and second element should be path
            where content after requesting URL is stored.
        """
        time.sleep(1)

        async def fetch_and_save(link, path, session):
            async with await session.get(link) as response:
                # print(response.headers['Content-Length'])
                contents = await response.read()
                if contents.startswith(b'<!DOCTYPE'):
                    # Raise error if given html instead of expected txt file
                    raise EDGARQueryError("You hit the rate limit")
                make_path(os.path.dirname(path))
                with open(path, "wb") as f:
                    f.write(contents)

        conn = TCPConnector(limit=self.rate_limit)
        raw_client = ClientSession(connector=conn, headers={'Connection': 'keep-alive'})
        async with raw_client:
            client = RateLimitedClientSession(raw_client, self.rate_limit)
            tasks = [asyncio.ensure_future(fetch_and_save(link, path, client))
                     for link, path in inputs]
            for f in asyncio.as_completed(tasks):
                await f
