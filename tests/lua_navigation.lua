local navigation = dofile(MOD_PATH .. "/navigation.lua")
vector = {distance = function(a,b)
    return math.sqrt((a.x-b.x)^2+(a.y-b.y)^2+(a.z-b.z)^2)
end}
local start, support, goal = {x=7,y=15,z=-4}, {x=8,y=15,z=-5}, {x=8,y=15,z=-7}
local options = {max_jump=1,max_drop=1,clear_height=2}
local calls, initial_error, support_error = 0, "pos1_too_high", nil
rp_pathfinder = {find_path = function(pos, target, radius, opts, timeout)
    calls = calls + 1
    assert(target==goal and radius==24 and opts==options and timeout==0.35)
    if vector.distance(pos,start)==0 then
        if initial_error then return nil, initial_error end
        return {start, goal}
    end
    assert(vector.distance(pos,support)==0, "must use the actual support")
    if support_error then return nil, support_error end
    return {support,goal}
end}
local contact = {type="node",axis="y",node_pos={x=8,y=14,z=-5},old_velocity={x=0,y=0,z=0}}
local ground = {touching_ground=true,collisions={contact}}
local function find(result)
    calls=0
    return navigation.find_path(start,goal,24,options,0.35,result)
end
local path,reason,index = find(ground)
assert(path and index==1 and calls==2, "corner recovery must visit the support waypoint")
initial_error="pos1_blocked"
path,reason,index=find(ground)
assert(path and index==1, "rounded centre inside a neighbouring node can also recover")
initial_error=nil
path,reason,index=find(ground)
assert(path and index==2 and calls==1, "normal route must remain unchanged")
initial_error="pos1_too_high"
assert(not find(nil) and calls==1, "do not recover without a collision report")
ground.touching_ground=false
assert(not find(ground) and calls==1, "do not redirect a body in midair")
ground.touching_ground=true
contact.axis="x"
assert(not find(ground) and calls==1, "a wall is not a supporting surface")
contact.axis="y"; contact.old_velocity.y=4
assert(not find(ground) and calls==1, "a ceiling is not a supporting surface")
contact.old_velocity.y=0; contact.type="object"
assert(not find(ground) and calls==1, "do not invent terrain under another entity")
contact.type="node"; contact.node_pos={x=20,y=14,z=-5}
assert(not find(ground) and calls==1, "ignore stale distant contacts")
contact.node_pos={x=8,y=14,z=-5}
support_error="no_path"
assert(not find(ground) and calls==2, "do not move toward an unreachable route")
initial_error="pos2_blocked"
assert(not find(ground) and calls==1, "a blocked goal must not trigger recovery")
