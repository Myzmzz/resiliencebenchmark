"""Parser for application environment YAML files."""

from pathlib import Path
from typing import List, Optional

import yaml

from backend.models.application import (
    Application,
    ApplicationDetails,
    CriticalPath,
    ImageInfo,
    NamespaceConfig,
    ReadinessGap,
    ReadinessInfo,
    SLO,
)


def parse_applications(repo_path: Path) -> List[Application]:
    """
    Parse all application YAML files from environment/applications/.

    Args:
        repo_path: Path to the resiliencebenchmark repository

    Returns:
        List of Application objects
    """
    applications_dir = repo_path / "environment" / "applications"

    if not applications_dir.exists():
        return []

    applications = []

    for yaml_file in applications_dir.glob("*.yaml"):
        try:
            app = _parse_application_file(yaml_file)
            if app:
                applications.append(app)
        except Exception as e:
            # Log error but continue processing other files
            print(f"Error parsing {yaml_file}: {e}")
            # Create error placeholder
            applications.append(
                Application(
                    name=yaml_file.stem,
                    displayName=yaml_file.stem,
                    benchmarkRole="unknown",
                    visibility="unknown",
                    namespace=NamespaceConfig(
                        template="unknown", lifecycle="unknown"
                    ),
                    imageCount=0,
                    imagePolicy="unknown",
                    criticalPathsCount=0,
                    sloCount=0,
                    status="error",
                    readinessStatus=f"parse_error: {str(e)}",
                    knownGaps=[],
                )
            )

    return applications


def _parse_application_file(yaml_file: Path) -> Optional[Application]:
    """Parse a single application YAML file."""
    with open(yaml_file, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not data or "metadata" not in data or "spec" not in data:
        return None

    metadata = data["metadata"]
    spec = data["spec"]

    # Parse namespace config
    namespace_data = spec.get("namespace", {})
    namespace = NamespaceConfig(
        template=namespace_data.get("template", ""),
        liveReference=namespace_data.get("liveReference"),
        lifecycle=namespace_data.get("lifecycle", ""),
    )

    # Count images
    image_lock = spec.get("imageLock", {})
    images = image_lock.get("images", [])
    image_count = len(images)
    image_policy = image_lock.get("policy", "unknown")

    # Count critical paths
    workloads = spec.get("workloads", {})
    critical_paths = workloads.get("criticalPaths", [])
    critical_paths_count = len(critical_paths)

    # Count SLOs
    slos = spec.get("slos", [])
    slo_count = len(slos)

    # Parse readiness
    readiness_data = spec.get("readiness", {})
    readiness_status = readiness_data.get("currentStatus", "unknown")

    known_gaps_data = readiness_data.get("knownGaps", [])
    known_gaps = [
        ReadinessGap(
            observedAt=gap.get("observedAt"),
            severity=gap.get("severity", "unknown"),
            item=gap.get("item", ""),
        )
        for gap in known_gaps_data
    ]

    # Determine overall status
    status = _determine_status(readiness_status, known_gaps)

    # Build details
    readiness_info = None
    if readiness_data:
        # Convert date objects to strings for JSON serialization
        resolved_issues = readiness_data.get("resolvedIssues")
        if resolved_issues:
            for issue in resolved_issues:
                if "resolvedAt" in issue and hasattr(issue["resolvedAt"], "isoformat"):
                    issue["resolvedAt"] = issue["resolvedAt"].isoformat()

        known_gaps_data = readiness_data.get("knownGaps", [])
        for gap in known_gaps_data:
            if "observedAt" in gap and hasattr(gap["observedAt"], "isoformat"):
                gap["observedAt"] = gap["observedAt"].isoformat()

        readiness_info = ReadinessInfo(
            currentStatus=readiness_status,
            knownGaps=known_gaps_data,
            resolvedIssues=resolved_issues,
            nextChecks=readiness_data.get("nextChecks"),
        )

    details = ApplicationDetails(
        sourceSnapshot=spec.get("sourceSnapshot"),
        imageLock=image_lock,
        workloads=workloads,
        slos=[SLO(**slo) for slo in slos] if slos else None,
        observability=spec.get("observability"),
        resetContract=spec.get("resetContract"),
        qualifyContract=spec.get("qualifyContract"),
        readiness=readiness_info,
    )

    return Application(
        name=metadata.get("name", yaml_file.stem),
        displayName=metadata.get("displayName", metadata.get("name", yaml_file.stem)),
        benchmarkRole=metadata.get("benchmarkRole", "unknown"),
        visibility=metadata.get("visibility", "unknown"),
        namespace=namespace,
        imageCount=image_count,
        imagePolicy=image_policy,
        criticalPathsCount=critical_paths_count,
        sloCount=slo_count,
        status=status,
        readinessStatus=readiness_status,
        knownGaps=known_gaps,
        details=details,
    )


def _determine_status(
    readiness_status: str, known_gaps: List[ReadinessGap]
) -> str:
    """
    Determine overall application status based on readiness.

    Returns: "qualified" | "partial" | "pending" | "inactive"
    """
    status_lower = readiness_status.lower()

    # Check for inactive status
    if "inactive" in status_lower or "standby" in status_lower:
        return "inactive"

    # Check for blocking gaps
    blocking_gaps = [g for g in known_gaps if g.severity == "blocking"]

    if blocking_gaps:
        return "pending"
    elif known_gaps:
        return "partial"
    elif "qualified" in status_lower or "ready" in status_lower:
        return "qualified"
    else:
        return "pending"
