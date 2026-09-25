"""Receipt tokens, native channel and MCP-plugin sidecar credentials."""

from __future__ import annotations

from theater.daemon.events.publication import next_revision, participant_event
from theater.daemon.persistence.store_parts._host import StoreHost
from theater.harness.contracts.channels import ChannelKind
from theater.models import Participant, now


class CredentialStore(StoreHost):
    """Store-facing credentials methods; state lives on ``Store``."""

    def set_receipt_token(
        self,
        participant_id: str,
        token: str,
        *,
        token_path: str | None = None,
    ) -> None:
        self._receipts.set_token(participant_id, token, token_path=token_path)

    def get_receipt_token(self, participant_id: str) -> str | None:
        return self._receipts.get_token(participant_id)

    def renew_receipt_token(self, participant_id: str) -> None:
        self._receipts.renew_token(participant_id)

    def delete_receipt_token(self, participant_id: str) -> None:
        self._receipts.delete_token(participant_id)

    def cleanup_receipt_tokens(self) -> int:
        return self._receipts.cleanup_tokens()

    # ---- native channel credentials -----------------------------------

    def set_channel_credential(
        self,
        participant_id: str,
        *,
        harness: str,
        kind: ChannelKind,
        channel_id: str,
        token: str,
        token_path: str,
    ) -> None:
        """Persist one generic native channel credential."""
        self._channels.set(
            participant_id,
            harness=harness,
            kind=kind,
            channel_id=channel_id,
            token=token,
            token_path=token_path,
        )

    def get_channel_credential(
        self,
        participant_id: str,
        kind: ChannelKind,
        channel_id: str,
    ):
        """Read one generic native channel credential."""
        return self._channels.get(participant_id, kind, channel_id)

    def delete_channel_credentials(self, participant_id: str) -> None:
        """Delete all generic native channel credentials for one participant."""
        self._channels.delete_participant(participant_id)

    def cleanup_channel_credentials(self) -> int:
        return self._channels.cleanup()

    # ---- MCP-plugin sidecar credentials -------------------------------

    def set_mcp_plugin_credential(
        self,
        participant_id: str,
        *,
        plugin_name: str,
        api_version: int,
        credential_id: str,
        credential_verifier: str,
        grants,
        credential_path: str,
    ) -> None:
        """Persist one sidecar verifier and its exact launch-time grants."""
        self._mcp_plugins.set(
            participant_id,
            plugin_name=plugin_name,
            api_version=api_version,
            credential_id=credential_id,
            credential_verifier=credential_verifier,
            grants=grants,
            credential_path=credential_path,
        )

    def get_mcp_plugin_credential(self, credential_id: str):
        """Return an active sidecar credential record by its public selector."""
        return self._mcp_plugins.get_by_credential_id(credential_id)

    def mcp_plugin_credentials(self, participant_id: str):
        """Return durable attached-sidecar facts for one participant."""
        return self._mcp_plugins.list_for_participant(participant_id)

    def delete_mcp_plugin_credential(self, participant_id: str, plugin_name: str) -> None:
        """Revoke one sidecar before it can use its credential again."""
        self._mcp_plugins.delete_plugin(participant_id, plugin_name)

    def delete_mcp_plugin_credentials(self, participant_id: str) -> None:
        """Revoke every sidecar credential belonging to a participant."""
        self._mcp_plugins.delete_participant(participant_id)

    def cleanup_mcp_plugin_credentials(self) -> int:
        return self._mcp_plugins.cleanup()

    def record_transcript_receipt(
        self,
        participant_id: str,
        *,
        session_id: str,
        transcript_location: str,
    ) -> Participant | None:
        """Atomically persist exact receipt provenance for a participant."""
        with self.write_unit() as unit:
            before = self._participants.get(participant_id, connection=unit.connection)
            participant = self._receipts.record_transcript_receipt(
                participant_id,
                session_id=session_id,
                transcript_location=transcript_location,
                connection=unit.connection,
            )
            if participant is not None and participant != before:
                self.journal.append_group(
                    unit,
                    [
                        participant_event(
                            self,
                            participant,
                            unit.connection,
                            revision=next_revision(self, unit.connection),
                            recorded_at=now(),
                        )
                    ],
                )
            return participant
