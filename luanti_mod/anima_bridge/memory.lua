local storage = minetest.get_mod_storage()
local KEY = "memory.v1"
local WORLD_KEY = "world.v1"
local MAX_EPISODES = 200
local MAX_WORLD_CELLS = 1000

local memory = minetest.deserialize(storage:get_string(KEY) or "")
if type(memory) ~= "table" then
    memory = {version = 1, episodes = {}}
end
memory.version = 1
memory.episodes = memory.episodes or {}

local world = minetest.deserialize(storage:get_string(WORLD_KEY) or "")
if type(world) ~= "table" then
    world = {version = 1, cells = {}, order = {}}
end
world.version = 1
world.cells = world.cells or {}
world.order = world.order or {}

local api = {}

local function save()
    storage:set_string(KEY, minetest.serialize(memory))
end

function api.remember(kind, data)
    table.insert(memory.episodes, {
        time = minetest.get_gametime(),
        kind = kind,
        data = data or {},
    })
    while #memory.episodes > MAX_EPISODES do
        table.remove(memory.episodes, 1)
    end
    save()
end

function api.count()
    return #memory.episodes
end

function api.last()
    return memory.episodes[#memory.episodes]
end

local function object_signature(objects)
    local parts = {}
    for _, object in ipairs(objects or {}) do
        table.insert(parts, table.concat({
            tostring(object.kind or ""),
            tostring(object.name or ""),
            tostring(math.floor(tonumber(object.distance) or 0)),
        }, ":"))
    end
    table.sort(parts)
    return table.concat(parts, "|")
end

function api.observe_world(position, nodes, objects)
    local key = string.format("%d:%d:%d", position.x, position.y, position.z)
    local previous = world.cells[key]
    local is_new = previous == nil
    local signature = object_signature(objects)
    local objects_changed = is_new or not previous.object_signature
        or previous.object_signature ~= signature
    world.cells[key] = {
        position = {
            x = position.x,
            y = position.y,
            z = position.z,
        },
        nodes = nodes or {},
        objects = objects or {},
        object_signature = signature,
        last_seen = minetest.get_gametime(),
    }
    if is_new then
        table.insert(world.order, key)
        while #world.order > MAX_WORLD_CELLS do
            local oldest = table.remove(world.order, 1)
            world.cells[oldest] = nil
        end
    end
    storage:set_string(WORLD_KEY, minetest.serialize(world))
    return is_new, objects_changed
end

function api.world_count()
    return #world.order
end

return api
