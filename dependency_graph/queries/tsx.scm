; JSX-only capture deltas for the LocAgent-TS graph builder (tsx grammar).
; Loaded IN ADDITION TO typescript.scm when the file is parsed with the 'tsx'
; grammar (.tsx / .jsx / .js). Consumed by dependency_graph/ts_build_graph.py.
;
;   @jsx.element  -- a JSX opening / self-closing tag. ts_build_graph reads its
;                    tag name (PascalCase -> name-matched against component nodes
;                    to build the `renders` edge, the UI analogue of `invokes`)
;                    AND its expression-valued attributes, which are recorded on
;                    the edge as prop -> bound-expression (onSave={handleSave}).
;                    lowercase tags are host DOM elements and are ignored.

(jsx_opening_element) @jsx.element

(jsx_self_closing_element) @jsx.element
