# Schema conventions

Use the project's existing labels and properties. Task, Decision, Insight and
Question are useful conventions, not a required schema.
{ .grag-lead }

## Reuse before defining

`describe_schema` shows the current vocabulary. `define_schema` rejects a new
name that differs only by case, plural or punctuation and suggests the existing
table. `allow_similar=true` permits an intentional similar name.

## Predictable schema names


**New in 0.10.0:** schema names use letters, digits and underscores,
starting with a letter or underscore. Grag reserves leading underscores for
internal tables/properties, and rejects words reserved by its pinned Ladybug
grammar, case-insensitively. This applies to node/relationship names, properties,
primary keys and relationship endpoints. For example, `optional` returns a
`schema_error` naming the property and suggesting `optional_value`; adding
backticks or setting `allow_similar=true` does not bypass validation.

The policy follows the [Ladybug 0.20.3 grammar](https://github.com/LadybugDB/ladybug/blob/v0.20.3/src/antlr4/Cypher.g4),
not a blanket list of SQL keywords. That grammar permits names such as `Match`,
`Return`, `Type` and `Limit`. Ordinary identifiers and custom typed primary keys
retain their behavior. Raw Cypher has the database's own quoting rules; grag's
schema API and generated statements use the simpler naming convention.

The entire schema request validates before its first schema change. Tables,
relationships and grag's table registry then commit together: a later invalid
endpoint, conflicting table or failed statement rolls back earlier changes in
that batch. Correct the reported issue and retry the complete request. Existing
graphs and unrelated memories are preserved. If native transaction completion is
uncertain, follow the returned reopen guidance and inspect the stored schema;
do not assume either a commit or rollback. Normal `if_not_exists=true` remains
idempotent and does not add missing properties to an existing table.




## Primary keys and properties

Primary keys belong in `key`, never `properties`. Undeclared, reserved or
mismatched properties are skipped with warnings; inspect the write response.
Use [guarded writes](mutations.md) to revise an existing record safely.
