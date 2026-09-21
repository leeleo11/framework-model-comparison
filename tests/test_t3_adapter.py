from baselines.t3_smolagents.adapter import normalize_code_agent_output


def test_normalize_deepseek_dsml_python_action_to_code_block():
    raw = """思考\n<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="python_interpreter">
<｜｜DSML｜｜ parameter name="code" string="true">print('ok')
</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>"""

    assert normalize_code_agent_output(raw) == "<code>\nprint('ok')\n</code>"


def test_normalize_dsml_final_answer_to_code_action():
    raw = """<｜｜DSML｜｜ calls>
<｜｜DSML｜｜ invoke name="final_answer">
<｜｜DSML｜｜ parameter name="answer" string="true">完成模型
</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>"""

    assert normalize_code_agent_output(raw) == "<code>\nfinal_answer('完成模型')\n</code>"


def test_normalize_keeps_native_code_block_unchanged():
    raw = "<code>\nprint('ok')\n</code>"

    assert normalize_code_agent_output(raw) == raw


def test_normalize_leaves_unrecognized_output_for_native_error_handling():
    raw = "plain text without an action"

    assert normalize_code_agent_output(raw) == raw
