ctx.push(new QuantizeV2Op(),
         { {% for tname in inputs[:-1]%} "{{tname}}", {% endfor %}"{{inputs[-1]}}" },
         { {% for tname in outputs[:-1]%} "{{tname}}", {% endfor %}"{{outputs[-1]}}" });