Pin both Docker base stages to a verified multi-platform digest and constrain
the complete Python runtime install, including PostgreSQL/MySQL drivers.
CI and the advisory scan cover those driver pins; Dependabot tracks the base.
(#176, #237, #261)
