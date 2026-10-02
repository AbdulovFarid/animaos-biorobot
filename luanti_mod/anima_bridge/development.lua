-- Pure blueprint/contract helpers, also exercised without a running world.
local api = {}

local function finite(n)
    return type(n) == "number" and n == n and math.abs(n) < math.huge
end

function api.valid_spec(spec)
    return type(spec) == "table"
        and (spec.half_size == 2 or spec.half_size == 3)
        and (spec.wall_height == 3 or spec.wall_height == 4)
        and (spec.door == "north" or spec.door == "south"
            or spec.door == "east" or spec.door == "west")
end

function api.valid_config(config)
    if type(config) ~= "table" or not finite(config.revision)
            or config.revision < 0 or config.revision % 1 ~= 0
            or not api.valid_spec(config.construction)
            or type(config.body) ~= "table" or type(config.gathering) ~= "table" then
        return false
    end
    local speed, cooldown = config.body.walk_speed, config.body.jump_cooldown
    return finite(speed) and speed >= 1.1 and speed <= 1.7
        and finite(cooldown) and cooldown >= 0.8 and cooldown <= 1.5
        and type(config.gathering.prefer_memory) == "boolean"
end

function api.door_offset(spec, offset)
    local n = spec.half_size + (offset or 0)
    if spec.door == "south" then return 0, n end
    if spec.door == "east" then return n, 0 end
    if spec.door == "west" then return -n, 0 end
    return 0, -n
end

function api.blueprint(site, spec)
    assert(api.valid_spec(spec), "invalid blueprint")
    local plan = {}
    local door_x, door_z = api.door_offset(spec)
    for y = 0, spec.wall_height - 1 do
        for x = -spec.half_size, spec.half_size do
            for z = -spec.half_size, spec.half_size do
                if (math.abs(x) == spec.half_size or math.abs(z) == spec.half_size)
                        and not (x == door_x and z == door_z) then
                    table.insert(plan, {x = site.x + x, y = site.y + y, z = site.z + z})
                end
            end
        end
    end
    for x = -spec.half_size, spec.half_size do
        for z = -spec.half_size, spec.half_size do
            table.insert(plan, {x = site.x + x, y = site.y + spec.wall_height, z = site.z + z})
        end
    end
    return plan
end

function api.inspect(site, spec, solid, empty)
    local solid_sensor, empty_sensor = solid, empty
    solid = function(p) return solid_sensor(p) == true end
    empty = function(p) return empty_sensor(p) == true end
    local q = {support = true, interior = true, doorway = true, roof = true, walls = true, access = true}
    local dx, dz = api.door_offset(spec)
    for x = -spec.half_size, spec.half_size do
        for z = -spec.half_size, spec.half_size do
            q.support = q.support and solid({x = site.x + x, y = site.y - 1, z = site.z + z})
            q.roof = q.roof and solid({x = site.x + x, y = site.y + spec.wall_height, z = site.z + z})
            for y = 0, spec.wall_height - 1 do
                local p = {x = site.x + x, y = site.y + y, z = site.z + z}
                if x == dx and z == dz then
                    q.doorway = q.doorway and empty(p)
                elseif math.abs(x) == spec.half_size or math.abs(z) == spec.half_size then
                    q.walls = q.walls and solid(p)
                else
                    q.interior = q.interior and empty(p)
                end
            end
        end
    end
    local ex, ez = api.door_offset(spec, 1)
    q.access = solid({x = site.x + ex, y = site.y - 1, z = site.z + ez})
        and empty({x = site.x + ex, y = site.y, z = site.z + ez})
        and empty({x = site.x + ex, y = site.y + 1, z = site.z + ez})
    local valid = true
    for _, value in pairs(q) do valid = valid and value end
    return valid, q
end

return api
