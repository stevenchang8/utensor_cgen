ctx.push(new ReluOp<{{in_dtype}}, {{out_dtype}}, {{qout_dtype}}>(), 
         { {% for tname in inputs[:-1]%}"{{tname}}", {% endfor %}"{{inputs[-1]}}" },
         { {% for tname in outputs[:-1]%}"{{tname}}", {% endfor %}"{{outputs[-1]}}" });