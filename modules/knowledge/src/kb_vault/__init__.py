"""KB v0.1 file-first knowledge vault."""

from .core import ConflictError, KBError, KnowledgeVault
from .atlas import ContentProjection, PublicAtlas
from .remote_inbox import GitHubRemoteInbox
from .curator import KnowledgeCurator
from .cycle import KnowledgeCycle
from .retrieval import TrustedRetrieval
from .migration import (
    migrate_instance,
    migration_plan,
    prepare_migration_source,
    retirement_plan,
    retire_source,
)
from .capability import KnowledgeCapabilities
from .model import (
    classification_level_for_tier,
    compatibility_tier_for,
    highest_classification_level,
    materialize_orthogonal_fields,
    validate_orthogonal_fields,
)
from .semantic import (
    governance_requirements,
    materialize_semantic_fields,
    validate_semantic_fields,
)

__all__ = [
    "ConflictError",
    "ContentProjection",
    "GitHubRemoteInbox",
    "KBError",
    "KnowledgeVault",
    "KnowledgeCurator",
    "KnowledgeCycle",
    "KnowledgeCapabilities",
    "TrustedRetrieval",
    "migration_plan",
    "migrate_instance",
    "prepare_migration_source",
    "retirement_plan",
    "retire_source",
    "PublicAtlas",
    "classification_level_for_tier",
    "compatibility_tier_for",
    "highest_classification_level",
    "governance_requirements",
    "materialize_orthogonal_fields",
    "materialize_semantic_fields",
    "validate_orthogonal_fields",
    "validate_semantic_fields",
]
