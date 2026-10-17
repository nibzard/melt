Melt IR — A language for agent-native interfaces

I think we’ve found one of the most important architectural foundations for Melt.

Instead of teaching agents to build applications, give them a language for describing computational experiences—and a system that turns those descriptions into real, interactive software.

The interesting combination is:

* Pele’s philosophy: A concise description becomes a sophisticated visual representation.
* Vega-Lite’s philosophy: A declarative grammar describes visualizations, composition, and interaction.
* Melt’s opportunity: A declarative grammar describes entire interactive computational environments that agents and humans can continuously modify.

I would call the foundation Melt IR (Intermediate Representation).

It would sit between an agent’s intentions and the running software.

1. The fundamental architecture

Agent / human authoring

Natural language, Python SDK, structured edits, direct manipulation

Melt IR

Typed, declarative, versioned, inspectable

Compiler

Validate · resolve · check authority · optimize · lower

Diagram

Pele / SVG

Visualization

Chart grammar

Controls and data

Native UI

Custom interactions

Extension runtime

Running computational environment

Native interaction · deterministic state updates · authorized computation

The compiler is the critical boundary. The agent proposes changes to software; it doesn’t directly control the renderer or acquire permissions by describing them.

The running environment remains operational when the agent is idle, offline, or replaced by a different model.

2. Don’t build a universal UI language

This is the first important design decision.

I would not try to create a giant JSON specification capable of describing every button, visual element, calculation, graph, event, animation, and layout property.

That would eventually become another frontend framework—just one that is harder to use.

Instead, Melt IR should be a small language for composing specialized languages.

Each specialized renderer has its own well-designed vocabulary.

Domain	Specialized representation	Compiler or renderer
Graphs, diagrams, workflows	Structured graph or Mermaid	Pele
Analytical visualizations	Marks, encodings, selections	Vega-Lite-like grammar
Forms and controls	Typed values, validation, presentation	Native form renderer
Tables	Data bindings, columns, sort/filter state	Native virtualized table
Documents	Markdown with semantic references	Document renderer
Workspace composition	Pane identities and spatial constraints	Native layout engine
Computation	Typed capability calls	Python or another authorized runtime

Existing work validates the usefulness of this decomposition. JSON Forms⁠￼ separates data schemas from UI schemas; Vega-Lite⁠￼ compiles concise visualization descriptions into a lower-level visualization system; and Pele⁠￼ offers synchronous diagram parsing and SVG rendering.

What none of those individually provides is Melt’s larger ambition: a common model of durable objects, actions, state, permissions, and relationships across different representations.

That’s what Melt IR must own.

3. The five primitives

I would preserve and refine the five primitives from our previous architecture discussion.

Primitive	Contract
Object	A typed, identifiable piece of information or a relationship, with versioned source references
Capability	Executable behavior with typed inputs, outputs, effects, and permissions
Projection	A view of objects and state, described using a specialized rendering grammar
Binding	A typed relationship between events, state changes, capabilities, and outputs
Workspace	The durable composition of objects, projections, bindings, state, and history

An important distinction: a projection isn’t the object itself.

A supplier can appear simultaneously as a node in a graph, a row in a table, and a record in an inspector. Those representations must refer to the same supplier identity.

And an executable capability isn’t tied to a particular control. A button, keyboard shortcut, graph selection, or agent can invoke the same operation through a common contract.

This is what enables composition without building a new bespoke application for every task.

4. Let’s design one real representation

Start with a supplier-risk workspace.

The agent has created a network of suppliers and products. The user can click a supplier to inspect it, then ask the agent to add a simulation that predicts the effect of losing that supplier.

Here’s what a proposed high-level Melt representation could look like.

Illustrative Melt IR v0.1 syntax—not an implemented schema.

version: melt/v0.1
id: supplier-workspace
sources:
  network:
    artifact: supplier-network
    version: 7
  suppliers:
    artifact: supplier-records
    version: 3
state:
  selected_supplier:
    type: supplier_id?
    initial: null
    persist: workspace
projections:
  - id: network
    renderer: diagram.flowchart
    source: network
    events:
      node.select:
        set: selected_supplier
        from: event.nodeId
  - id: inspector
    renderer: record
    source: suppliers
    key: $state.selected_supplier
    controls:
      - id: simulate
        kind: button
        label: Simulate outage
        invoke: simulate_outage
        args:
          supplier_id: $state.selected_supplier
  - id: impact
    renderer: table
    source:
      latest_success: simulate_outage
capabilities:
  simulate_outage:
    implementation: python:actions/outage.py
    input:
      supplier_id: supplier_id
    output: table
    permission: execute_project_code
layout:
  split: [network, inspector]
  ratio: 0.65

Notice what isn’t here: HTML, CSS, JavaScript callbacks, AppKit code, or instructions addressed to the agent.

This representation describes what exists, how it relates, and what happens when someone interacts with it.

For the graph projection, Melt could lower the typed graph into Mermaid and ask Pele to render it. But the typed graph—not the generated SVG or Mermaid string—should remain the authoritative source of node identities.

Pele supplies rendering. Melt must supply stable node-to-object mapping, hit-testing or selection integration, and the event semantics. Those interactions are not magically provided by Pele.

What happens at runtime?

The direct-interaction path

User selects supplier node S-42

Typed event

network.node.select(nodeId = “S-42”)

Deterministic state reducer

selected_supplier = “S-42”

Incremental projection update

Graph highlights node; inspector displays S-42

No agent or Python process needed

Now the user clicks Simulate outage.

That’s a different type of interaction. It invokes a registered Python capability, subject to execution permission, produces a versioned result, and updates the impact table after successful publication.

The user can operate the entire instrument without asking the agent to reason again.

Then the user says:

Add a control that lets me choose two suppliers and compare the financial impact.

The agent modifies the representation, adds or updates the Python capability, and submits a revision-checked patch.

The system validates the changes and updates only the affected parts of the interface. Existing objects, user selections, and unaffected pane state survive.

This is the essential Melt experience: declarative software that the agent can evolve and the human can continue operating.

5. The compiler must produce more than pixels

This is where I would focus the engineering work.

The compiler consumes the declarative specification plus known artifact schemas, registered capabilities, renderer versions, and workspace context.

It produces several related outputs.

Compiled result	Responsibility
Render plan	Which renderer handles each projection and its configuration
State plan	Typed state variables, scopes, defaults, validation, and persistence
Interaction plan	Event schemas and deterministic state transitions
Capability plan	Registered callable behavior, typed inputs/outputs, and permission requirements
Dependency plan	Which projections must update when particular sources or state values change
Identity mapping	Stable connections between semantic objects and visible elements

This allows Melt to perform incremental updates without reinterpreting the entire interface every time something changes.

It also separates what can run immediately from what requires computation or authorization.

Three execution paths

Local

Pure interaction

Selecting, highlighting, resizing, switching tabs, changing visible state. Respond within the interactive frame budget.

Compute

Deterministic dispatch, potentially effectful execution

Invoke saved capabilities through the authorized runtime. Show progress, cancellation, and committed outputs.

Agent

Software evolution

Interpret a new requirement, propose changes to objects or capabilities, validate them, and publish a new version.

One subtlety matters: deterministic rendering does not make arbitrary Python computations deterministic.

The guarantee should be narrower and precise: for the same canonical representation, artifact versions, state, renderer versions, and rendering context, compilation produces equivalent interface behavior. Effectful computations are governed and recorded separately.

6. Agent-friendly is a design constraint, not just a compact syntax

Traditional developer frameworks optimize for humans writing code.

This representation language should optimize for agents creating correct software through small, testable changes.

I’d make these properties mandatory.

Property	Why it matters
Small semantic vocabulary	Reduces ambiguous generation and unnecessary tokens
Explicit stable identities	Enables incremental changes without reconstructing everything
Typed references	Prevents wiring incompatible sources, controls, and events together
Useful defaults	Agents specify intent instead of hundreds of visual details
Structured compiler errors	Agents can repair a precise failure instead of guessing
Incremental patches	Modify one behavior without regenerating the workspace
Introspection	Agents can ask what objects, bindings, and capabilities currently exist
Portable, versioned definitions	Generated software remains inspectable and migratable

For instance, rather than rejecting a generated interface with “invalid specification,” the compiler could return:

E_BINDING_TYPE
Path: projections.network.events.node.select
Expected: supplier_id
Received: row_index
Fix: Bind the event's nodeId to a stable
supplier identity, not its display position.

This is an important future advantage.

Agents become more reliable not only because models improve, but because the environment gives them a constrained language and precise feedback about their proposed changes.

I would also implement an inspection and dry-run interface that allows the agent to compile and examine the consequences of a proposal before publishing it.

7. We must preserve an escape hatch for genuinely new interfaces

This is the biggest long-term architectural decision.

A declarative vocabulary can be incredibly powerful, but eventually the user will ask for an interaction that no existing grammar supports.

Melt must have a path for that.

I propose two levels of extensibility.

Level A — Declarative composition. The agent creates new functionality by composing existing data representations, controls, relationships, and capabilities. This should be the fast, reliable default.

Level B — Custom renderer modules. The agent can propose a genuinely new interactive component. That component implements a standard contract defining its input schema, renderer, emitted events, accessibility behavior, and interaction with Melt state.

The interface might be based on a sandboxed component runtime in the future. It should not allow generated code to acquire arbitrary native application privileges simply by registering itself as a renderer.

The essential contract would look conceptually like:

interface MeltRenderer<Spec, Event> {
  validate(spec: unknown): Spec;
  render(spec: Spec, context: RenderContext): RenderOutput;
  describeEvents(): EventSchema<Event>;
}

That’s an illustrative interface, not the final runtime ABI. Real custom interactive renderers also need lifecycle, state, focus, accessibility, resource limits, and isolation contracts.

We should design the extension boundary now, but build the first compiler around trusted built-in renderers.

The meta-grammar must be extensible without requiring every generated interface to become arbitrary executable frontend code.

8. How this fits Ghostty, Pi Durable, and Python

The important thing is to avoid throwing away the architecture you’ve already developed.

Ghostty / Swift

Own the host, native surfaces, focus, direct interaction, and common projections. Render compiled plans rather than agent-generated native code.

Pi Durable

Own the agent lifecycle, tool orchestration, committed workspace changes, and recovery. Agents propose Melt IR changes through tools.

Python

Define executable capabilities and high-level typed authoring helpers. Publish artifacts and descriptions, not live interpreter objects.

Melt compiler

Become the independent boundary connecting all three. Validate the IR, construct plans, dispatch to renderers, and preserve identity across revisions.

I would make the compiled representation independent of Pi conversation history and Ghostty’s view objects. That portability will become increasingly important as Melt grows.

9. The first implementation experiment

Rather than immediately build a complete Melt IR, I would prove the architecture through one narrow vertical slice.

Melt IR v0

Proposed milestone

One graph, one inspector, one capability, one working compiler.

Define the minimal typed IR

Source references, state, projections, bindings, capabilities and a simple layout

Build a compiler and renderer adapter

Validate and lower a typed graph to Pele, with native event handling

Prove the direct interaction loop

Select a node and update a linked inspector without a model call

Prove agent-driven evolution

Add an outage simulation and a result table through a revision-checked IR patch

Prove continuity

Restart offline and recover the interface, definitions, saved result and interaction state

0 of 5 milestones complete

Copy checklist

We should measure interaction latency, compilation latency, preservation of identity during patches, and failure behavior. The existing spec’s local-feedback and committed-update performance targets are reasonable starting budgets, not proven numbers.

10. The design principle I’d lock now

Agents specify. Compilers realize. Humans operate.

Agents can change the specification. Humans can directly manipulate its running realization. Both act on the same underlying, durable objects and capabilities.

This leads to the most important requirement for Melt IR:

It must be bidirectional at the semantic level.

Compilation flows from a declarative description into the interface. Human edits flow back as typed changes to objects, state, and capability definitions—not by trying to reverse-engineer generated pixels or SVG.

For example, moving a diagram node may modify presentation state. Changing a supplier relationship modifies the underlying graph. Requesting a new simulation changes executable functionality. Those are different operations, and the representation must distinguish them.

That distinction is what could make the whole environment coherent as software evolves.

My proposed next engineering artifact is the Melt IR v0.1 language specification: exact object schemas, state scopes, event contracts, compiler stages, renderer interfaces, patch semantics, and a complete supplier-workspace test fixture.

The goal is not yet to create another UI framework. It’s to create the smallest language that lets an agent evolve working software without sacrificing the speed, predictability, and ownership of traditional applications.
