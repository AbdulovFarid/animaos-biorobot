-- Deterministic flat-world adapter fixture, not an engine physics simulation.
local nodes, events, commands, steps, stored = {}, {}, {}, {}, {}
local now, body, command = 0, nil, nil
local shutdown, deferred, heartbeat = {}, {}, false
local config = {revision=0, body={walk_speed=1.5,jump_cooldown=0.9},
    gathering={prefer_memory=true},construction={half_size=2,wall_height=3,door="north"}}
local function key(p) return math.floor(p.x+0.5)..":"..math.floor(p.y+0.5)..":"..math.floor(p.z+0.5) end
vector = {
    zero=function() return {x=0,y=0,z=0} end,
    round=function(p) return {x=math.floor(p.x+0.5),y=math.floor(p.y+0.5),z=math.floor(p.z+0.5)} end,
    distance=function(a,b) return math.sqrt((a.x-b.x)^2+(a.y-b.y)^2+(a.z-b.z)^2) end,
}
local player = {get_pos=function() return {x=0,y=0,z=0} end}
local function noop() end
minetest = {
    get_current_modname=function() return "anima_bridge" end,
    get_modpath=function() return MOD_PATH end,
    get_worldpath=function() return "test-world" end,
    mkdir=function() error("Mod security: mod directory writes are forbidden") end,
    settings={get_bool=function(_,key,default)
        if key=="anima_bridge_autostart" then return false end
        return default
    end},
    after=function(_,fn) table.insert(deferred,fn) end,
    register_on_shutdown=function(fn) table.insert(shutdown,fn) end,
    register_on_joinplayer=noop,
    register_on_chat_message=noop,
    get_mod_storage=function() return {
        get_string=function(_,k) return stored[k] or "" end,
        set_string=function(_,k,v) stored[k]=v end,
        set_int=function(_,k,v) stored[k]=tostring(v) end,
    } end,
    serialize=function(v) return v end,
    deserialize=function(v) return type(v)=="table" and v or nil end,
    log=noop,
    chat_send_player=noop,
    write_json=function(event) table.insert(events,event); return "event" end,
    get_gametime=function() return now end,
    get_timeofday=function() return 0.5 end,
    get_connected_players=function() return {} end,
    get_player_by_name=function() return player end,
    get_objects_inside_radius=function() return {} end,
    get_item_group=function() return 0 end,
    dir_to_yaw=function() return 0 end,
    registered_nodes={air={walkable=false,buildable_to=true},dirt={walkable=true},
        ["rp_default:tree"]={walkable=true},["rp_default:planks"]={walkable=true}},
    get_node_or_nil=function(p) return {name=nodes[key(p)] or (p.y<0 and "dirt" or "air")} end,
    set_node=function(p,n) nodes[key(p)]=n.name end,
    remove_node=function(p) nodes[key(p)]=nil end,
    find_nodes_in_area=function(minp,maxp,names)
        local result={}
        for k,n in pairs(nodes) do
            if n=="rp_default:tree" then
                local x,y,z=k:match("([^:]+):([^:]+):([^:]+)")
                table.insert(result,{x=tonumber(x),y=tonumber(y),z=tonumber(z)})
            end
        end
        return result
    end,
    register_entity=noop,
    register_chatcommand=function(name,def) commands[name]=def end,
    register_globalstep=function(fn) table.insert(steps,fn) end,
    add_entity=function(pos)
        body={pos=pos, velocity=vector.zero(), get_pos=function(self) return self.pos end,
            get_velocity=function(self) return self.velocity end,
            set_velocity=function(self,v) self.velocity=v end,
            set_acceleration=function(self,a) self.acceleration=a end,
            set_animation=noop,set_yaw=noop,set_nametag_attributes=noop}
        return body
    end,
    request_insecure_environment=function()
        assert(debug.getinfo(2,"S").source:match("/init.lua$"), "trusted I/O must be requested from init.lua")
        return {
        io={open=function(path)
            if path==MOD_PATH.."/runtime/heartbeat" then
                return {write=function() heartbeat=true end,close=noop}
            end
            if path==MOD_PATH.."/runtime/development.json" then
                return {read=function() return "config" end,close=noop}
            end
            if path==MOD_PATH.."/runtime/command.json" and command then
                return {read=function() return "command" end,close=noop}
            end
        end},
        os={remove=function(path)
            if path==MOD_PATH.."/runtime/heartbeat" then heartbeat=false end
        end,execute=function() error("Startup tests must not launch Python") end},
    } end,
    parse_json=function(text)
        if text=="config" then return config end
        local result=command; command=nil; return result
    end,
}
minetest.registered_chatcommands=commands
rp_pathfinder={find_path=function(start,goal) return {start,goal} end}
local function step()
    now=now+0.25
    for _,fn in ipairs(steps) do fn(0.25) end
    if body then
        body.pos.x=body.pos.x+body.velocity.x*0.25
        body.pos.z=body.pos.z+body.velocity.z*0.25
    end
end
local function last_event(kind)
    for i=#events,1,-1 do if events[i].kind==kind then return events[i].data end end
end
dofile(MOD_PATH.."/init.lua")
for _,fn in ipairs(deferred) do fn() end
assert(heartbeat, "init.lua must write the heartbeat via trusted I/O")
assert(commands.anima_spawn.func("singleplayer"))
for _=1,8 do step() end
assert(last_event("development_applied").revision==0)
for y=0,8 do
    nodes[key({x=4,y=y,z=0})]="rp_default:tree"
    nodes[key({x=4,y=y,z=2})]="rp_default:tree"
end
assert(commands.anima_build.func("singleplayer","start"))
for _=1,180 do
    step()
    if last_event("build_completed") then break end
end
assert(last_event("resource_gathered"), "gathering must produce an outcome")
local completed=last_event("build_completed")
assert(completed, "building did not finish")
assert(completed.quality.interior and completed.quality.roof and completed.quality.access)
assert(completed.development_revision==0)
assert(stored.home_site.spec.half_size==2, "home must retain its own dimensions")
config={revision=1,body={walk_speed=1.3,jump_cooldown=1.1},
    gathering={prefer_memory=false},construction={half_size=3,wall_height=4,door="east"}}
for _=1,8 do step() end
assert(last_event("development_applied").revision==1)
assert(stored.home_site.spec.half_size==2, "new blueprint must not change old home geometry")
command={action="rest"}
for _=1,90 do step() end
assert(last_event("rest_completed"), "standing still after rest must report recovery")
local ok,message=commands.anima_evolution.func("singleplayer","status")
assert(ok and message:find("1",1,true))
assert(commands.anima_evolution.func("singleplayer","rollback"))
assert(last_event("development_request").action=="rollback")
assert(commands.anima_auto.func("singleplayer","off"))
body.velocity={x=0,y=-3,z=0}
assert(commands.anima_move.func("singleplayer","north"))
assert(body.velocity.y==-3 and body.acceleration.y<0, "manual movement must preserve falling physics")
-- No horizontal progress: the delayed monitor must stop walking, not gravity.
deferred[#deferred]()
assert(body.velocity.y==-3 and body.acceleration.y<0, "manual stop must preserve falling physics")
assert(body.velocity.x==0 and body.velocity.z==0)
for _,fn in ipairs(shutdown) do fn() end
assert(not heartbeat, "shutdown must remove the heartbeat")
assert(last_event("world_shutdown"), "shutdown must notify the host bridge")
