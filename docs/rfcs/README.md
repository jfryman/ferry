# Design proposals (RFCs)

Larger changes are proposed here before they are built, so the design can be reviewed apart from
the code. An RFC describes intended behaviour. The rest of `docs/` describes what ferry does
today. Until an RFC is marked **Implemented**, nothing in it exists in ferry.

| RFC | Title | Status |
|---|---|---|
| [0001](0001-audit-trail.md) | Audit trail and audit API ([OpenAPI contract](../api/audit.openapi.yaml)) | Proposed |

## Writing one

- Name the file `NNNN-short-title.md`, with the next free number.
- Start with a status table: **Proposed**, **Accepted**, **Implemented** or **Withdrawn**.
- Say what exists today, with the code it lives in, before what should change.
- List open questions at the end, for the maintainers to answer.
- API contracts go under [`docs/api/`](../api/) as OpenAPI 3.1. Lint them with
  `npx @redocly/cli lint docs/api/<file>.yaml` and keep the lint free of warnings.
- Record the decision in [lode/decisions.md](../../lode/decisions.md) once it is accepted.
