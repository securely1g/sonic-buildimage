"""Expose a target's configured dependency labels for declared graph tests."""

_Labels = provider(fields = ["labels"])

def _collect(target, ctx):
    children = []
    if ctx.rule:
        for name in dir(ctx.rule.attr):
            # Implicit compiler/tool dependencies may carry separate host-side
            # Rust crates; only aggregate the consumer's declared graph.
            if name.startswith("_"):
                continue
            value = getattr(ctx.rule.attr, name)
            if type(value) == "dict":
                values = value.keys() + value.values()
            else:
                values = value if type(value) == "list" else [value]
            for child in values:
                if type(child) == "Target" and _Labels in child:
                    children.append(child[_Labels].labels)
    entry = [ctx.rule.kind + "\t" + str(target.label)] if ctx.rule else []
    return [_Labels(labels = depset(entry, transitive = children))]

_labels = aspect(implementation = _collect, attr_aspects = ["*"])

def _impl(ctx):
    output = ctx.actions.declare_file(ctx.label.name + ".txt")
    labels = depset(transitive = [target[_Labels].labels for target in ctx.attr.roots])
    ctx.actions.write(output, "\n".join(sorted(labels.to_list())) + "\n")
    return [DefaultInfo(files = depset([output]))]

configured_labels = rule(
    implementation = _impl,
    attrs = {"roots": attr.label_list(mandatory = True, aspects = [_labels])},
)
