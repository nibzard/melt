# RFC 002: Apps you can keep, reshape, and combine

Status: Proposed product direction; no changes to `melt/v0.1`.

A request for comments (RFC) records a proposed direction and the evidence needed to support it.
This RFC separates Melt's product promise from its longer-term architectural ambition.

[SPEC.md](../../SPEC.md) remains the implementation contract.
[RFC 001](001-semantic-interactions.md) remains a separate language extension.
This proposal does not change either document's compatibility rules or delivery gates.

## 1. Decision summary

Adopt this proposed mission:

> Melt lets you create apps you can keep, reshape, and combine—without losing your work.

Lead with the experience of owning a useful app:

> Describe the tool you need. Make it yours. Keep using it. Share it.

Use a narrow first application to test this promise.
Do not permanently define Melt as a dashboard product.
Do not require the first application to prove a universal platform.

Treat a reusable foundation as a conclusion supported by evidence.
The important evidence is continued use, independent composition, and successful updates around existing work.

## 2. Problem

Creating an interface is only the start of useful software.
People also need to reopen it, change it, share it, and maintain it after it contains real work.

A conversation can help create a tool.
The conversation must not be the only place where the tool's definitions, state, or behavior exist.
The resulting app needs an independent lifetime.

The central hypothesis is that changing software can become part of using it.
The user should retain their information, history, and control through those changes.

This promise does not make every interaction a conversation.
People use controls directly for ordinary work.
They ask an agent when they need a new capability or a structural change.

## 3. Three distinct promises

| Promise | Meaning | Evidence |
|---|---|---|
| Malleability | Change an app without losing existing work. | Add a control or behavior while preserving compatible data, state, and results. |
| Composition | Different tools cooperate through shared information and behavior. | An independently built tool uses existing records through published contracts. |
| Portability | Move or share an app without depending on its original agent or host. | A compatible recipient opens a defined export with explicit dependencies. |

Each promise needs separate evidence.
Saving and reopening prove persistence.
Adding a control proves a limited form of malleability.
Neither alone proves composition or portability.

Using the app without its original agent does not imply that every operation works offline.
Computations can require an execution environment.
External operations can require a network connection and separate authorization.

## 4. The first product experience

Use this recurring task to guide the café application:

> I check café profit every week. Make me an app I can keep.

The proposed experience has seven steps:

1. Open the Agent and supply the café's data.
2. Ask for a dashboard in a separate app window.
3. Add cost, break-even, and opening-day controls through conversation.
4. Adjust the scenario and save the app to the desktop.
5. Close both the Agent and the dashboard.
6. Open the saved app from its icon with the same controls, values, results, and layout.
7. Use the app directly, or choose **Edit with Agent** or **Share app**.

The desktop icon gives the result a clear identity outside the conversation.
Reopening shows that it remains useful after the Agent leaves.
Preserving local changes during a later update would provide stronger evidence of ownership.

The [café concept film](../../demos/cafe-animation/README.md) illustrates this experience with mock screens and synthetic data.
It does not prove desktop installation, sharing, or native recovery.
Those claims require implementation and separate acceptance checks.

## 5. What an app contains

The proposed product model is an app with definitions and explicit data dependencies.
It is not a new serialized format in this RFC.

Definitions describe the views, controls, calculations, and behavior that make the app useful.
Dependencies identify the information and execution resources that those definitions need.
Saved state and published results preserve the user's working context.

A data dependency can reference shared records or include an immutable snapshot.
The app must distinguish authoritative records, external references, derived results, and historical snapshots.
Copying a snapshot does not make the copy authoritative.

An app is a product concept; a workspace is an existing runtime concept.
Do not assume that every app requires a separate copy of all workspace data.
The mapping between apps, workspaces, and shared records needs a later technical proposal.

### 5.1 Sharing has three meanings

| Operation | Recipient receives | Required distinction |
|---|---|---|
| Share the tool | Definitions and dependency requirements | The recipient supplies or connects their own data. |
| Share a copy | Definitions, selected data, and saved state | The copy is independent and has a stated snapshot boundary. |
| Share live work | Access to existing records | Access changes authority; it does not create an independent copy. |

The product must explain which operation the user selects.
A generic **Share app** action must not silently choose between them.

Exports do not transfer credentials or execution grants.
Recipients receive an explicit account of included data, required environments, and unresolved dependencies.
Opening saved results must not silently execute project code or external effects.

Packaging, dependency resolution, and shared access need separate contracts before implementation.

## 6. The composition experiment

A second chart in the café dashboard provides limited evidence of generality.
Use a separate menu pricing tool to test composition.

After defining the necessary record mutation contract, ask another developer to build the tool against published interfaces.
Do not modify the core specifically to accommodate that implementation.

Verify this sequence:

1. Open a café app that already contains ingredients, recipes, and saved calculations.
2. Add the independently built menu pricing tool.
3. Resolve its dependencies against the existing ingredients and recipes through explicit mappings.
4. Change an ingredient price through an authorized record operation.
5. Verify that both tools resolve the same updated record and identify affected results as stale until recalculated.
6. Recalculate and inspect the input versions used by each result.
7. Remove the pricing tool and verify that the café's records and history remain intact.

The tools must not create silent, disconnected copies of the same records.
Removing a view or tool must not implicitly delete shared information.
Deleting shared information needs its own explicit operation and dependency checks.

This experiment requires more than the identity index in RFC 001.
That RFC does not define edits to domain records.
Define ownership, conflicts, revisions, publication, and authorization for record changes before attempting this experiment.
Keep the new behavior outside `melt/v0.1` and require explicit migration.

## 7. Architecture questions to resolve through evidence

The broader vision raises six questions.
Treat them as design constraints for concrete workflows, not a checklist for immediate platform development.

| Area | Question |
|---|---|
| Identity and ownership | Which records do tools share, who owns them, and how do explicit mappings preserve their meaning? |
| State and transitions | Which changes affect presentation, domain records, or external systems? |
| Change proposals | Can the user understand what changes, what survives, and which results or operations become invalid? |
| Capability boundaries | How does execution enforce authority independently of app definitions and generated code? |
| History and evolution | How do migrations and updates preserve existing work and its historical meaning? |
| Extensions | How does a custom view participate in shared identity, state, events, and lifecycle rules? |

Preserve the existing separation between compilation, execution, publication, and rendering.
Keep authority outside ordinary app descriptions.
Record external effects honestly; reverting a definition cannot undo a sent order.
Reuse suitable rendering, storage, and execution infrastructure behind explicit contracts.

Do not assume matching field names imply matching meanings.
Do not require a universal domain model or one database for all information.

## 8. Evidence and delivery sequence

Continue the existing Linux and native delivery gates.
This RFC does not move native work ahead of required runtime checks.
New language behavior needs a separate proposal and migration contract.

Use these product milestones to decide what to pursue next:

| Stage | Experiment | Evidence to record |
|---|---|---|
| Useful app | Build a tool for a recurring task. | Task completion and repeated use by its intended users. |
| Independent lifetime | Save, close, reopen, and operate it. | Preserved state and results; no model call or project execution during restoration. |
| Adaptation | Change an app after it contains real work. | Explicit preview, retained compatible state, and recovery from rejected changes. |
| Composition | Add the independently built pricing tool. | Core changes, adapter effort, duplicated state, and maintainer intervention. |
| Maintenance | Apply an upstream fix after a local customization. | Preserved customization, explicit conflicts, and recovery after a failed update. |

For maintenance, customize the scenario controls, then apply a fix from the calculation's original author.
Keep the customization and existing records.
Identify saved results that need recalculation without rewriting their historical inputs.
Require a decision for incompatible changes rather than silently dropping the customization.

Record failures as evidence, including manual repairs and work performed by the core team.
Set acceptance criteria before each experiment and report effort with the result.
If every new tool requires substantial core changes, the foundation has not yet demonstrated reuse.

Compare the full lifecycle with a conventional generated app.
Include operation, adaptation, updates, recovery, and composition, not just initial creation.

## 9. Non-goals

This RFC does not propose:

- A universal runtime for all categories of software.
- A new language identifier, package format, or record mutation schema.
- Automatic semantic migrations or conflict-free updates.
- A widget catalog as evidence of composition.
- Immediate multi-user synchronization or a public extension marketplace.
- A requirement that users become software maintainers.

Most users should be able to operate a maintained tool with little customization.
More control should become available when their work requires it.

## 10. Open decisions

- Does saving create an independent app or a launcher for an existing workspace?
- How do app identity and shared record identity differ when an app is copied?
- Which state and artifacts belong in a portable copy by default?
- How does a recipient inspect an app whose execution environment is unavailable?
- How do app versions, dependency versions, and local customizations interact during an update?
- Which host compatibility guarantees are worth testing before supporting a second host?

Resolve these through the staged experiments and focused technical proposals.
Keep the public promise simple while the architecture earns broader claims.
