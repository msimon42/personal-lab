"""Lazy EU5 registration; domain code remains usable without the MCP SDK."""


def build_operations(application):
    from dataclasses import replace
    from pathlib import Path
    from backend.app.eu5_operations import EU5Operations, build_operations as domain_operations
    from toolkit_core.storage import Conflict, canonical, digest

    operations = domain_operations(application.service, profile=application.profile.name,
                                   installations=application.configuration.installations)
    result = []
    for operation in operations:
        if operation.name != "eu5_workspace_export":
            result.append(operation)
            continue
        def export(args, original=operation.handler):
            with application.service.command_lock:
                policy = application._policy_digest()
                exported = original(args)
                workspace = application.store.get("lab_eu5_workspace", args.workspace_id)
                if policy != application._policy_digest():
                    raise Conflict("Source disclosure changed during experimental export")
                inspector = EU5Operations(application.service, profile=application.profile.name)
                inspector._disclosure(inspector._snapshot(workspace["snapshot_id"]))
                scope = application._scope("eu5", {"snapshot_id": workspace["snapshot_id"]})
                binding_id = "eu5-export-" + digest(canonical({"artifact": exported.artifact_id, "scope": scope}))
                try:
                    binding = application.store.get("lab_eu5_export_binding", binding_id)
                except KeyError:
                    domain_artifact = application.store.get("artifact", exported.artifact_id)
                    relative = Path(domain_artifact["path"]).relative_to(application.service.root).as_posix()
                    raw = application.store.fs.read_bytes(relative)
                    if digest(raw) != exported.sha256:
                        raise Conflict("The exported bundle changed before artifact registration")
                    with application.store.atomic():
                        if policy != application._policy_digest():
                            raise Conflict("Source disclosure changed before artifact registration")
                        artifact = application.artifacts.register_bytes(raw, "application/zip", scope=scope)
                        binding = application.store.put("lab_eu5_export_binding", {"id": binding_id, "artifact_id": artifact.id})
                return exported.model_copy(update={"artifact_id": binding["artifact_id"]})
        result.append(replace(operation, handler=export))
    return result
