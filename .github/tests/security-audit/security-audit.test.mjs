// SPDX-FileCopyrightText: 2026 Daniel Radman
// SPDX-License-Identifier: Apache-2.0

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { Parser, Lexer, Evaluator, data } from "@actions/expressions";

const workflowPath = fileURLToPath(new URL("../../workflows/security-audit.yml", import.meta.url));
const workflow = JSON.parse(execFileSync(process.env.PYTHON || "python", [
  "-c",
  "import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))",
  workflowPath,
], { encoding: "utf8" }));
const job = workflow.jobs.dispatch;
const checkout = job.steps.find(step => step.uses?.startsWith("actions/checkout@"));

function evaluate(source, github) {
  const expression = source.replace(/^\$\{\{\s*/, "").replace(/\s*\}\}$/, "");
  const tokens = new Lexer(expression).lex().tokens;
  const parsed = new Parser(tokens, ["github"], []).parse();
  const context = JSON.parse(JSON.stringify({ github }), data.reviver);
  return new Evaluator(parsed, context).evaluate().coerceString();
}

const cases = [
  {
    "name": "schedule without repository payload",
    "event": "schedule",
    "ref": "refs/heads/main",
    "refName": "main",
    "payload": {},
    "runs": true,
    "branch": "main"
  },
  {
    "name": "schedule on renamed default branch",
    "event": "schedule",
    "ref": "refs/heads/trunk",
    "refName": "trunk",
    "payload": {},
    "runs": true,
    "branch": "trunk"
  },
  {
    "name": "schedule with empty repository metadata",
    "event": "schedule",
    "ref": "refs/heads/main",
    "refName": "main",
    "payload": {
      "repository": {}
    },
    "runs": true,
    "branch": "main"
  },
  {
    "name": "manual default branch",
    "event": "workflow_dispatch",
    "ref": "refs/heads/main",
    "refName": "main",
    "payload": {
      "repository": {
        "default_branch": "main"
      }
    },
    "runs": true,
    "branch": "main"
  },
  {
    "name": "manual renamed default branch",
    "event": "workflow_dispatch",
    "ref": "refs/heads/trunk",
    "refName": "trunk",
    "payload": {
      "repository": {
        "default_branch": "trunk"
      }
    },
    "runs": true,
    "branch": "trunk"
  },
  {
    "name": "manual feature branch refused",
    "event": "workflow_dispatch",
    "ref": "refs/heads/feature",
    "refName": "feature",
    "payload": {
      "repository": {
        "default_branch": "main"
      }
    },
    "runs": false
  },
  {
    "name": "manual same-named tag refused",
    "event": "workflow_dispatch",
    "ref": "refs/tags/main",
    "refName": "main",
    "payload": {
      "repository": {
        "default_branch": "main"
      }
    },
    "runs": false
  },
  {
    "name": "manual missing default metadata refused",
    "event": "workflow_dispatch",
    "ref": "refs/heads/main",
    "refName": "main",
    "payload": {},
    "runs": false
  }
];

for (const scenario of cases) {
  test(scenario.name, () => {
    const github = {
      event_name: scenario.event,
      ref: scenario.ref,
      ref_name: scenario.refName,
      event: scenario.payload,
    };
    assert.equal(evaluate(job.if, github), String(scenario.runs), "dispatch eligibility");
    if (scenario.runs) {
      assert.equal(evaluate(checkout.with.ref, github), scenario.branch, "checked-out branch");
    }
  });
}
