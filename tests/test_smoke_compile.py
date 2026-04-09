import py_compile


def test_compile_main_scripts():
    py_compile.compile("detrend_advisor.py", doraise=True)
    py_compile.compile("ringclim.py", doraise=True)
