# Copyright 2020 Google, LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# This sample creates a secure two-service application running on Cloud Run.
# This test builds and deploys the two secure services
# to test that they interact properly together.

import datetime
import os
import subprocess
import time
from urllib import request
import uuid

from google.cloud import logging_v2

import pytest
# Unique suffix to create distinct service names
SUFFIX = uuid.uuid4().hex
PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
IMAGE_NAME = f"gcr.io/{PROJECT}/logging-{SUFFIX}"


@pytest.fixture
def container_image():
    # Build container image for Cloud Run deployment
    subprocess.run(
        [
            "gcloud",
            "builds",
            "submit",
            "--tag",
            IMAGE_NAME,
            "--project",
            PROJECT,
            "--quiet",
        ], check=True
    )
    yield IMAGE_NAME

    # Delete container image
    subprocess.run(
        [
            "gcloud",
            "container",
            "images",
            "delete",
            IMAGE_NAME,
            "--quiet",
            "--project",
            PROJECT,
        ], check=True
    )

@pytest.fixture
def deployed_service(container_image):
    # Deploy image to Cloud Run
    service_name = f"logging-{SUFFIX}"
    subprocess.run(
        [
            "gcloud",
            "run",
            "deploy",
            service_name,
            "--image",
            container_image,
            "--region=us-central1",
            "--platform=managed",
            "--set-env-vars",
            f"GOOGLE_CLOUD_PROJECT={PROJECT}"
            "--no-allow-unauthenticated"

        ], check=True
    )

    yield service_name

    subprocess.run(
        [
            "gcloud",
            "run",
            "services",
            "delete",
            service_name,
            "--platform=managed",
            "--region=us-central1",
            "--quiet",
            "--project",
            PROJECT,
        ], check=True
    )


@pytest.fixture
def service_url_auth_token(deployed_service):
    # Get Cloud Run service URL and auth token
    service_url = subprocess.run(
        [
            "gcloud",
            "run",
            "services",
            "describe",
            deployed_service,
            "--platform=managed",
            "--region=us-central1",
            "--format=value(status.url)",
            "--project",
            PROJECT,
        ],
        stdout=subprocess.PIPE,
        check=True
    ).stdout.strip().decode()
    auth_token = subprocess.run(
        ["gcloud", "auth", "print-identity-token"],
        stdout=subprocess.PIPE,
        check=True
    ).stdout.strip().decode()

    yield service_url, auth_token


def test_end_to_end(service_url_auth_token, deployed_service):
    service_url, auth_token = service_url_auth_token

    # Test that the service is responding
    req = request.Request(
        service_url,
        headers={
            "Authorization": f"Bearer {auth_token}",
            "X-Cloud-Trace-Context": "foo/bar",
        },
    )
    response = request.urlopen(req)
    assert response.status == 200

    body = response.read()
    assert body.decode() == "Hello Logger!"

    # Test that the logs are writing properly to stackdriver
    time.sleep(10)  # Slight delay writing to stackdriver
    client = logging_v2.LoggingServiceV2Client()
    resource_names = [f"projects/{PROJECT}"]
    # We add timestamp for making the query faster.
    now = datetime.datetime.now(datetime.timezone.utc)
    filter_date = now - datetime.timedelta(minutes=1)
    filters = (
        f"timestamp>=\"{filter_date.isoformat('T')}\" "
        "resource.type=cloud_run_revision "
        "AND severity=NOTICE "
        f"AND resource.labels.service_name={deployed_service} "
        "AND jsonPayload.component=arbitrary-property"
    )

    # Retry a maximum number of 10 times to find results in stackdriver
    for x in range(10):
        iterator = client.list_log_entries(resource_names, filter_=filters)
        for entry in iterator:
            # If there are any results, exit loop
            break
        # Linear backoff
        time.sleep(3 * x)

    assert iterator.num_results
