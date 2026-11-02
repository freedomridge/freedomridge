#!/usr/bin/env python3
"""
Example script showing dataset creation and deletion workflow.

AUTHENTICATION:
This script uses API token authentication (Bearer tokens).

To get your API token:
1. Log in to the UI (e.g., http://localhost or your Freedom Ridge server URL)
2. Go to Settings → API Tokens
3. Click "Create New Token"
4. Give it a name (e.g., "my-laptop")
5. Copy the token value
6. Set FRIDGE_API_TOKEN environment variable or paste it below

Usage:
    FRIDGE_API_TOKEN=your-token python3 examples/sdk/example_dataset_delete.py
"""

import argparse
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'python'))

from fridge.core.client import Client  # noqa: E402

API_URL = ''
session = requests.Session()
client: Client


def get_user_info():
    """Verify authentication by getting current user"""
    response = session.get(
        f"{API_URL}/auth/me",
    )

    if response.status_code != 200:
        print(f"❌ Authentication failed: {response.text}")
        print("\nYour token may be invalid or expired. Please:")
        print("1. Go to Settings → API Tokens in the UI")
        print("2. Create a new token")
        print("3. Use the new token")
        sys.exit(1)

    user = response.json()
    print(f"✅ Authenticated as: {user['username']} (role: {user['role']})")
    return user


def run_dataset_lifecycle_test():
    """Test creating and deleting a dataset"""

    print("\n" + "=" * 60)
    print("DATASET LIFECYCLE TEST")
    print("=" * 60)

    # 1. Create dataset
    print("\n1️⃣  Creating test dataset...")
    create_response = session.post(
        f"{API_URL}/datasets",
        json={
            "name": "test_delete_dataset",
            "description": "Temporary dataset for testing deletion",
            "media_fields": ["filepath"],
        },
    )

    if create_response.status_code != 200:
        print(f"❌ Failed to create dataset: {create_response.text}")
        return False

    dataset = create_response.json()
    dataset_id = dataset["id"]
    print(f"✅ Created dataset ID: {dataset_id}")
    print(f"   Name: {dataset['name']}")
    print(f"   Description: {dataset['description']}")

    # 2. Add a sample
    print("\n2️⃣  Adding test sample...")
    sample_response = session.post(
        f"{API_URL}/datasets/{dataset_id}/samples",
        json=[
            {
                "filepath": "/tmp/test_image.jpg",
                "tags": ["test"],
                "metadata": {"purpose": "deletion_test"},
            },
        ],
    )

    if sample_response.status_code != 200:
        print(f"❌ Failed to add sample: {sample_response.text}")
        return False

    print("✅ Added test sample")

    # 3. Verify dataset exists
    print("\n3️⃣  Verifying dataset exists...")
    verify_response = session.get(
        f"{API_URL}/datasets/{dataset_id}",
    )

    if verify_response.status_code != 200:
        print(f"❌ Dataset not found: {verify_response.text}")
        return False

    print(f"✅ Dataset verified (ID: {dataset_id})")

    # 4. List samples to confirm
    print("\n4️⃣  Listing samples...")
    samples_response = session.get(
        f"{API_URL}/datasets/{dataset_id}/samples",
    )

    if samples_response.status_code == 200:
        samples_data = samples_response.json()
        print(f"✅ Found {samples_data['total']} sample(s)")

    # 5. Delete dataset. DELETE answers 202 with a server job; the client waits for it.
    print("\n5️⃣  Deleting dataset (server job)...")
    try:
        delete_result = client.delete_dataset(dataset_id)
    except Exception as e:
        print(f"❌ Failed to delete dataset: {e}")
        return False

    print("✅ Deleted dataset")
    print(f"   Message: {delete_result.get('message', 'No message')}")

    # 6. Verify deletion
    print("\n6️⃣  Verifying deletion...")
    verify_deleted = session.get(
        f"{API_URL}/datasets/{dataset_id}",
    )

    if verify_deleted.status_code == 404:
        print("✅ Dataset successfully deleted (404 Not Found)")
        return True
    else:
        print("❌ Dataset still exists after deletion!")
        print(f"   Status code: {verify_deleted.status_code}")
        return False


def main():
    global API_URL, session, client
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-url', default=os.getenv('FRIDGE_API_URL', 'http://localhost:8000/api'))
    args = parser.parse_args()
    if not os.getenv('FRIDGE_API_TOKEN'):
        parser.error('Set FRIDGE_API_TOKEN from Settings → API Tokens')
    API_URL = args.api_url
    client = Client(API_URL)
    session = client.session
    print("Dataset Creation & Deletion Test")
    print("=" * 60)

    # Verify authentication
    user = get_user_info()

    # Check permissions: creating and deleting datasets are admin-only
    if user['role'] != 'admin':
        print("\n❌ Insufficient permissions")
        print(f"   Your role: {user['role']}")
        print("   Required: admin")
        return 1

    # Run test
    success = run_dataset_lifecycle_test()

    # Summary
    print("\n" + "=" * 60)
    if success:
        print("✅ ALL TESTS PASSED")
        print("=" * 60)
        return 0
    else:
        print("❌ TESTS FAILED")
        print("=" * 60)
        return 1


if __name__ == "__main__":
    sys.exit(main())
