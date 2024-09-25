#  Copyright (c) ZenML GmbH 2024. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at:
#
#       https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
#  or implied. See the License for the specific language governing
#  permissions and limitations under the License.
"""ZenML Pro tenant models."""

from typing import Optional
from uuid import UUID

from pydantic import Field

from zenml.login.pro.models import BaseRestAPIModel
from zenml.login.pro.organization.models import OrganizationRead
from zenml.utils.enum_utils import StrEnum


class TenantStatus(StrEnum):
    """Enum that represents the desired state or status of a tenant.

    These values can be used in two places:

    * in the `desired_state` field of a tenant object, to indicate the desired
    state of the tenant (with the exception of `PENDING` and `FAILED` which
    are not valid values for `desired_state`)
    * in the `status` field of a tenant object, to indicate the current state
    of the tenant
    """

    # Tenant hasn't been deployed yet (i.e. newly created) or has been fully
    # deleted by the infrastructure provider
    NOT_INITIALIZED = "not_initialized"
    # Tenant is being processed by the infrastructure provider (is being
    # deployed, updated, deactivated, re-activated or deleted/cleaned up).
    PENDING = "pending"
    # Tenant is up and running
    AVAILABLE = "available"
    # Tenant is in a failure state (i.e. deployment, update or deletion failed)
    FAILED = "failed"
    # Tenant is deactivated
    DEACTIVATED = "deactivated"
    # Tenant resources have been deleted by the infrastructure provider but
    # the tenant object still exists in the database
    DELETED = "deleted"


class TenantStatusReason(StrEnum):
    """Enum that represents the reason for the status of a tenant.

    Sometimes, tenants are put in a certain state because of a specific internal
    reason and not because the user explicitly requested it. This enum
    represents those reasons.
    """

    # The tenant was put in this state because of a direct user action
    USER_ACTION = "user_action"
    # Tenant is deactivated because it was inactive for too long
    INACTIVE = "inactive"
    # Tenant is deactivated because the subscription has expired or was
    # cancelled
    SUBSCRIPTION_ENDED = "subscription_ended"


class TenantRead(BaseRestAPIModel):
    """Pydantic Model for viewing a Tenant."""

    id: UUID

    name: str
    description: Optional[str] = Field(
        default=None, description="The description of the tenant."
    )

    organization: OrganizationRead

    desired_state: str = Field(description="The desired state of the tenant.")
    state_reason: str = Field(
        description="The reason for the current tenant state.",
    )
    status: str = Field(
        description="The current operational state of the tenant."
    )
