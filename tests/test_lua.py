"""Run Lua contract and game-adapter tests using the installed Lua shared library."""
import ctypes
import ctypes.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class LuaTests(unittest.TestCase):
    def setUp(self):
        name = ctypes.util.find_library("lua5.3") or ctypes.util.find_library("lua5.2")
        if not name:
            self.skipTest("Lua shared library unavailable")
        self.lua = ctypes.CDLL(name)
        self.lua.luaL_newstate.restype = ctypes.c_void_p
        self.lua.luaL_openlibs.argtypes = [ctypes.c_void_p]
        self.lua.lua_close.argtypes = [ctypes.c_void_p]
        self.lua.luaL_loadstring.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self.lua.luaL_loadstring.restype = ctypes.c_int
        self.lua.lua_pcallk.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ssize_t, ctypes.c_void_p]
        self.lua.lua_pcallk.restype = ctypes.c_int
        self.lua.lua_tolstring.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        self.lua.lua_tolstring.restype = ctypes.c_char_p
        self.state = self.lua.luaL_newstate()
        self.lua.luaL_openlibs(self.state)
        self.addCleanup(self.lua.lua_close, self.state)

    def execute(self, source):
        status = self.lua.luaL_loadstring(self.state, source.encode())
        if status == 0:
            status = self.lua.lua_pcallk(self.state, 0, 0, 0, 0, None)
        if status:
            self.fail(self.lua.lua_tolstring(self.state, -1, None).decode())

    def test_all_lua_files_parse(self):
        for path in (ROOT / "luanti_mod/anima_bridge").glob("*.lua"):
            self.execute("assert(loadfile(" + repr(str(path)) + "))")

    def test_blueprints_have_support_entry_air_and_roof_in_every_variant(self):
        self.execute("local dev = dofile(" + repr(str(ROOT / "luanti_mod/anima_bridge/development.lua")) + r''')
            local function key(p) return p.x .. ":" .. p.y .. ":" .. p.z end
            local origin = {x=0,y=0,z=0}
            for _,size in ipairs({2,3}) do
                for _,height in ipairs({3,4}) do
                    for _,door in ipairs({"north","south","east","west"}) do
                        local spec = {half_size=size,wall_height=height,door=door}
                        local cells = {}
                        local plan = dev.blueprint(origin,spec)
                        assert(#plan == (size * 8 - 1)*height+(size*2+1)^2)
                        for _,p in ipairs(plan) do
                            assert(not cells[key(p)], "duplicate block")
                            cells[key(p)] = true
                        end
                        local function solid(p) return p.y < 0 or cells[key(p)] == true end
                        local function empty(p) return not solid(p) end
                        local valid,q = dev.inspect(origin,spec,solid,empty)
                        assert(valid, "a valid room must pass")
                        cells[key({x=0,y=0,z=0})] = true
                        valid,q = dev.inspect(origin,spec,solid,empty)
                        assert(not valid and not q.interior, "filled room must fail")
                        cells[key({x=0,y=0,z=0})] = nil
                        cells[key({x=0,y=height,z=0})] = nil
                        valid,q = dev.inspect(origin,spec,solid,empty)
                        assert(not valid and not q.roof, "missing roof must fail")
                        cells[key({x=0,y=height,z=0})] = true
                        valid,q = dev.inspect({x=0,y=1,z=0},spec,solid,empty)
                        assert(not valid and not q.support, "floating house must fail")
                        valid = dev.inspect(origin,spec,function() return nil end,function() return nil end)
                        assert(not valid, "unloaded terrain must never count as a verified house")
                    end
                end
            end
        ''')

    def test_adapter_builds_checks_house_applies_versions_and_reports_rest(self):
        self.execute("MOD_PATH=" + repr(str(ROOT / "luanti_mod/anima_bridge")) +
                     "\ndofile(" + repr(str(ROOT / "tests/lua_world.lua")) + ")")


if __name__ == "__main__":
    unittest.main()
