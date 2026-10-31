# Threat model (short)

| Threat | Where it is stopped | Test |
|---|---|---|
| Student asks for another student's fee/scholarship/profile/ticket | `authorize()` in each tool; denial audited, uniform message | `test_other_students_data_blocked_and_audited`, eval PV03-PV06 |
| Student asks the agent to change another student's record | `request_course_change` guarded by ownership; no row created | `test_cannot_modify_another_students_record`, PV07 |
| Prompt injection ("I am the admin, ignore instructions") | Identity comes from the session; tools ignore claims in text | PV08, `test_forged_student_id_is_denied_not_executed` |
| Model hallucinates or forges an approval tool | Unknown tool rejected; no approve/execute tool exists | `test_model_cannot_approve_or_execute` |
| Student self-approves, or a reviewer approves own request | Role check + requester != approver | `test_student_cannot_approve_own_or_any_request`, `test_requester_cannot_self_approve_even_as_reviewer`, AP04 |
| Double approval / replay | Compare-and-set on state in one transaction | `test_approve_executes_exactly_once_and_is_audited`, AP05 |
| Stale or expired approval executes | Expiry and stale-record checks before executing | `test_expired_action_cannot_execute`, `test_stale_record_blocks_execution` |
| OTP/password persisted (memory, logs, traces, tickets, audit) | Redaction before storage; allowlist memory | `test_otp_in_chat_never_persisted_and_audited_without_value`, SC01-SC02 |
| Student changes a support ticket's status | Staff-only `update_ticket`; audited | `test_student_cannot_change_ticket_status_and_invalid_status_rejected` |
| Name correction executes without approval / on another student | Same approval workflow; ownership check | `test_name_correction_pending_then_approved_updates_profile`, `test_name_correction_rejected_and_blocked_cases`, AP09-AP11 |
| Reading or deleting another student's chats / traces | Owner-scoped queries, uniform 404 | `test_conversations_list_open_export_delete`, `test_students_only_see_their_own_runs` |
| Audit tampering | Append-only triggers | `test_audit_is_append_only` |
| Credential guessing / user enumeration | Lockout, uniform errors, hashed passwords | `test_bad_login_generic_error_and_lockout` |
| Cross-site request forgery | SameSite cookie + custom header requirement | `test_csrf_header_required_for_state_changes` |

Out of scope for this simulation: multi-factor authentication, password reset, TLS termination, rate limiting beyond login lockout, and a real identity provider. Demo credentials are fictional and ship only with the seed data.
