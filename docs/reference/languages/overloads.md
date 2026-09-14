# Java and C# overload identities

[All language support](index.md) · [Index code](../../guides/code.md)

## Stable function IDs


Function IDs use `<module>#<qualified-name>~<signature-digest>` for **all** Java/C#
functions, so adding or removing an overload does not rename the others.
`identity_signature` records the normalized declaration syntax: parameter types,
generic arity and relevant constructor/interface distinctions. The digest excludes
line numbers, formatting, comments, local parameter names, defaults and bodies.
The normal `signature` property retains the full declaration header, including
multiline parameter lists. Type aliases, generic substitution and compiler type
equivalence are not evaluated. Other languages retain their existing IDs.

## Review records from an older index

On the first refreshed scan, old managed Java/C# Function records are retained
with `_source_state="obsolete"`, including records without graph links: they may
contain authored properties or history. Generated containment is rebuilt for the
new identities. Authored/unknown relationships remain attached to the old record;
grag cannot infer which overload the author intended after an earlier ID collision.
The ingest response explains this transition. Current search excludes the obsolete
records; Cypher and `evidence="all"` can inspect them.

To review replacements for an old ID, query current candidates, compare their
signatures and citations, then explicitly reconcile any authored references:

```cypher
MATCH (f:Function)
WHERE f.id STARTS WITH '<old-function-id>~' AND f._source_state = 'current'
RETURN f.id, f.signature, f.path, f.line_start, f.identity_signature
```

Legacy records are intentionally retained until reviewed; ingestion does not
silently delete or retarget them. New overloads use ordinary source-deletion
handling, preserving obsolete nodes when authored references still need them.
This improves structural identities, not Java/C# call resolution.
