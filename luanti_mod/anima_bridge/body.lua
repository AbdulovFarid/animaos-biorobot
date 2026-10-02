local modname = minetest.get_current_modname()
local insecure_environment = ... -- supplied only by this mod's init.lua
local memory = dofile(minetest.get_modpath(modname) .. "/memory.lua")
local development = dofile(minetest.get_modpath(modname) .. "/development.lua")
local development_revision = -1
local development_summary = {}
local prefer_resource_memory = true
local walk_speed = 1.5
local build_spec = {half_size = 2, wall_height = 3, door = "north"}
local failed_build_sites = {}
local rest_elapsed = nil
local jump_origin = nil
local jump_revision = nil
local jump_observed_elapsed = 0
local path_goal = nil
local path_revision = nil
local last_path_failure_at = -10
local ani_object = nil
local move_token = 0
local storage = minetest.get_mod_storage()
local HUNGER_MAX = 20
local SATURATION_MAX = 100
local hunger = tonumber(storage:get_string("hunger")) or HUNGER_MAX
local saturation = tonumber(storage:get_string("saturation")) or 0
local need_elapsed = 0.0
local food_check_elapsed = 0.0
local autonomy_enabled = false
local autonomy_elapsed = 0.0
local path = nil
local path_index = 1
local path_stuck_elapsed = 0.0
local path_last_pos = nil
local path_target_label = nil
local jump_active = false
local jump_elapsed = 0.0
local jump_cooldown = 0.0
local last_moveresult = nil
local target_food = nil
local target_food_name = nil
local target_tree_apple = nil
local social_target = nil
local social_player_name = nil
local social_time_left = 0.0
local social_return_building = false
local social_return_exploration = true
local SOCIAL_HOLD_DISTANCE = 2.2
local SOCIAL_DURATION = 20.0
local building_enabled = false
local build_state = "idle"
local build_site = nil
local build_plan = {}
local stored_home = storage:get_string("home_site")
local home_site = nil
if stored_home ~= "" and minetest.deserialize then
    home_site = minetest.deserialize(stored_home)
end
local home_spec = home_site and home_site.spec or {half_size = 2, wall_height = 3, door = "north"}
if not development.valid_spec(home_spec) then
    home_spec = {half_size = 2, wall_height = 3, door = "north"}
end
local safety_elapsed = 0.0
local safety_mode = nil
local safety_target = nil
local safety_last_key = nil
local SAFETY_SAMPLE_INTERVAL = 3.0
local NIGHT_START = 0.23
local NIGHT_END = 0.77
local SAFETY_PLAYER_RADIUS = 2.6
local TOUCH_PLAYER_RADIUS = 1.25
local build_index = 1
local build_resource_target = nil
local build_search_target = nil
local build_search_elapsed = 0.0
local build_log_name = nil
local build_material = "rp_default:planks"
local build_inventory = {logs = 0, planks = 0}
local build_resource_memory = {}
local saved_resources = storage:get_string("development.resources")
if saved_resources ~= "" then
    local loaded = minetest.deserialize(saved_resources)
    if type(loaded) == "table" then build_resource_memory = loaded end
end
local BUILD_RESOURCE_MEMORY_LIMIT = 24
local build_cooldown = 0.0
local BUILD_HALF_SIZE = 2
local BUILD_WALL_HEIGHT = 3
local BUILD_ROOF_LEVEL = BUILD_WALL_HEIGHT
local BUILD_PLANKS_NEEDED = 70
local BUILD_RESOURCE_RADIUS = 64
-- Luanti движет тело по центру модели, поэтому нужен небольшой запас до дерева.
local BUILD_RESOURCE_REACH = 4.0
local BUILD_TREE_NAMES = {
    "rp_default:tree",
    "rp_default:tree_oak",
    "rp_default:tree_birch",
    "rp_default:tree_fir",
    "rp_default:tree_poplar",
    "rp_default:tree_redwood",
}
local BUILD_PLANKS_BY_TREE = {
    ["rp_default:tree"] = "rp_default:planks",
    ["rp_default:tree_oak"] = "rp_default:planks_oak",
    ["rp_default:tree_birch"] = "rp_default:planks_birch",
    ["rp_default:tree_fir"] = "rp_default:planks_fir",
    ["rp_default:tree_poplar"] = "rp_default:planks_birch",
    ["rp_default:tree_redwood"] = "rp_default:planks",
}
local exploration_enabled = true
local exploration_target = nil
local exploration_elapsed = 0.0
local liquid_escape_target = nil
local liquid_escape_elapsed = 0.0
local observation_elapsed = 0.0
local OBJECT_OBSERVATION_RADIUS = 16
local PATH_SEARCH_DISTANCE = 24
local PATH_TIMEOUT = 0.35
local EXPLORE_RADIUS_MIN = 5
local EXPLORE_RADIUS_MAX = 10
local OBSERVATION_INTERVAL = 6.0
local APPLE_SEARCH_RADIUS = 24
local APPLE_REACH = 5.5
local JUMP_VELOCITY = 4.0
local JUMP_GRAVITY = -9.81
local JUMP_COOLDOWN = 0.9
local MAX_JUMP_TIME = 1.35
local RUNTIME_DIR = minetest.get_modpath(modname) .. "/runtime"
-- The host bridge creates this directory. Mod security forbids core.mkdir here,
-- even for trusted mods; trusted I/O below only reads the exchange files.
local AI_COMMAND_PATH = RUNTIME_DIR .. "/command.json"
local DEVELOPMENT_PATH = RUNTIME_DIR .. "/development.json"
local development_poll_elapsed = 0
local ai_command_elapsed = 0.0
local AI_COMMAND_POLL_INTERVAL = 0.25
local command_io = insecure_environment and insecure_environment.io
local command_os = insecure_environment and insecure_environment.os
local directions = {
    north = {x = 0, y = 0, z = -1},
    south = {x = 0, y = 0, z = 1},
    east = {x = 1, y = 0, z = 0},
    west = {x = -1, y = 0, z = 0},
}
local function is_walkable(pos)
    local node = minetest.get_node_or_nil(pos)
    if not node then return true end
    local def = minetest.registered_nodes[node.name]
    return def and def.walkable == true
end
local function blocked_ahead(pos)
    local cell = {x = math.floor(pos.x + 0.5), y = math.floor(pos.y + 0.5), z = math.floor(pos.z + 0.5)}
    local feet = {x = cell.x, y = cell.y, z = cell.z}
    local head = {x = cell.x, y = cell.y + 1, z = cell.z}
    return is_walkable(feet) or is_walkable(head)
end
local function collides_with_node_wall(moveresult)
    if not moveresult or not moveresult.collides or not moveresult.collisions then
        return false
    end
    for _, collision in ipairs(moveresult.collisions) do
        if collision.type == "node"
                and (collision.axis == "x" or collision.axis == "z") then
            return true
        end
    end
    return false
end
local function save_needs()
    storage:set_int("hunger", math.floor(hunger))
    storage:set_int("saturation", math.floor(saturation))
end
local function emit_world_event(kind, data)
    if not minetest.write_json then return end
    data = data or {}
    data.world_id = minetest.get_worldpath()
    if data.development_revision == nil then data.development_revision = development_revision end
    minetest.log("action", "[anima_event] " .. minetest.write_json({
        kind = kind,
        data = data or {},
    }))
end
local function observe_nearby_objects(origin)
    local objects = {}
    for _, object in ipairs(minetest.get_objects_inside_radius(origin, OBJECT_OBSERVATION_RADIUS)) do
        if object ~= ani_object then
            local object_pos = object:get_pos()
            if object_pos then
                local kind = nil
                local object_name = nil
                local nametag = nil
                if object:is_player() then
                    kind = "player"
                    object_name = object:get_player_name()
                else
                    local entity = object:get_luaentity()
                    if entity and entity.name
                            and entity.name ~= "__builtin:item"
                            and entity.name ~= modname .. ":body" then
                        kind = "entity"
                        object_name = entity.name
                        local properties = object:get_properties()
                        nametag = properties and properties.nametag or nil
                    end
                end
                if kind and object_name then
                    local distance = vector.distance(origin, object_pos)
                    table.insert(objects, {
                        kind = kind,
                        name = object_name,
                        nametag = nametag,
                        position = {
                            x = math.floor(object_pos.x * 10 + 0.5) / 10,
                            y = math.floor(object_pos.y * 10 + 0.5) / 10,
                            z = math.floor(object_pos.z * 10 + 0.5) / 10,
                        },
                        distance = math.floor(distance * 10 + 0.5) / 10,
                    })
                end
            end
        end
    end
    table.sort(objects, function(a, b)
        return a.distance < b.distance
    end)
    return objects
end

local function observe_world()
    if not ani_object or not ani_object:get_pos() then return end
    local pos = vector.round(ani_object:get_pos())
    local nodes = {}
    local seen = {}
    local resources = {}
    for dx = -2, 2 do
        for dy = -1, 6 do
            for dz = -2, 2 do
                local node = minetest.get_node_or_nil({
                    x = pos.x + dx,
                    y = pos.y + dy,
                    z = pos.z + dz,
                })
                if node and node.name and #resources < 32
                        and (node.name:find("apple", 1, true) or BUILD_PLANKS_BY_TREE[node.name]) then
                    table.insert(resources, {name = node.name,
                        position = {x = pos.x + dx, y = pos.y + dy, z = pos.z + dz}})
                end
                if node and node.name and not seen[node.name] then
                    seen[node.name] = true
                    table.insert(nodes, node.name)
                end
            end
        end
    end
    table.sort(nodes)
    local objects = observe_nearby_objects(ani_object:get_pos())
    local is_new, objects_changed = memory.observe_world(pos, nodes, objects)
    if is_new or objects_changed then
        memory.remember("world_observation", {
            position = pos,
            nodes = nodes,
            objects = objects,
        })
        minetest.log("action", string.format(
            "[anima_bridge] Ani observed cell=(%d,%d,%d) nodes=%d objects=%d",
            pos.x, pos.y, pos.z, #nodes, #objects))
    end
    emit_world_event("world_observation", {
        position = pos, nodes = nodes, objects = objects, resources = resources,
    })
end
local function try_eat_nearby()
    if not ani_object or not ani_object:get_pos() then return false end
    if hunger >= HUNGER_MAX and saturation >= SATURATION_MAX then return false end
    local objects = minetest.get_objects_inside_radius(ani_object:get_pos(), 1.5)
    for _, object in ipairs(objects) do
        local entity = object:get_luaentity()
        if entity and entity.name == "__builtin:item" and entity.itemstring and entity.itemstring ~= "" then
            local stack = ItemStack(entity.itemstring)
            local definition = stack:get_definition()
            local food = definition and tonumber(definition._rp_hunger_food) or 0
            local sat = definition and tonumber(definition._rp_hunger_sat) or 0
            if food > 0 then
                stack:take_item(1)
                if stack:is_empty() then
                    object:remove()
                elseif entity.set_item then
                    entity:set_item(stack)
                end
                hunger = math.min(HUNGER_MAX, hunger + food)
                saturation = math.min(SATURATION_MAX, saturation + sat)
                save_needs()
                memory.remember("eat", {
                    item = stack:get_name(),
                    hunger = hunger,
                    saturation = saturation,
                })
                minetest.log("action", string.format(
                    "[anima_bridge] Ani ate %s hunger=%d saturation=%d",
                    definition.description or stack:get_name(), hunger, saturation))
                emit_world_event("food_eaten", {
                    item = stack:get_name(),
                    hunger = food,
                    saturation = sat,
                })
                return true
            end
        end
    end
    return false
end
local function is_food_object(object)
    local entity = object and object:get_luaentity()
    if not entity or entity.name ~= "__builtin:item" or not entity.itemstring or entity.itemstring == "" then
        return false
    end
    local stack = ItemStack(entity.itemstring)
    local definition = stack:get_definition()
    local food = definition and tonumber(definition._rp_hunger_food) or 0
    return food > 0, stack:get_name()
end
local is_safe_exploration_cell
local stop_autonomous_motion
local clear_path
local function find_nearest_food()
    if not ani_object or not ani_object:get_pos() then return nil end
    if hunger >= HUNGER_MAX and saturation >= SATURATION_MAX then return nil end
    local origin = ani_object:get_pos()
    local nearest = nil
    local nearest_distance = nil
    for _, object in ipairs(minetest.get_objects_inside_radius(origin, 16)) do
        local is_food, item_name = is_food_object(object)
        if is_food then
            local object_pos = object:get_pos()
            local distance = vector.distance(origin, object_pos)
            if not nearest_distance or distance < nearest_distance then
                nearest = object
                nearest_distance = distance
                target_food_name = item_name
            end
        end
    end
    return nearest
end
local function is_tree_apple_node(name)
    return name == "rp_default:apple" or name == "rp_default:apple_floor"
end
local function find_tree_apple_approach(apple_pos, origin)
    local best = nil
    local best_distance = nil
    for dx = -2, 2 do
        for dz = -2, 2 do
            for y = apple_pos.y + 1, apple_pos.y - 10, -1 do
                local candidate = {
                    x = apple_pos.x + dx,
                    y = y,
                    z = apple_pos.z + dz,
                }
                if vector.distance(candidate, apple_pos) <= APPLE_REACH
                        and is_safe_exploration_cell(candidate) then
                    local distance = vector.distance(origin, candidate)
                    if not best_distance or distance < best_distance then
                        best = candidate
                        best_distance = distance
                    end
                end
            end
        end
    end
    return best
end
local function find_nearest_tree_apple()
    if not ani_object or not ani_object:get_pos() then return nil end
    if hunger >= HUNGER_MAX and saturation >= SATURATION_MAX then return nil end
    local origin = ani_object:get_pos()
    local minp = {
        x = math.floor(origin.x - APPLE_SEARCH_RADIUS),
        y = math.floor(origin.y - 12),
        z = math.floor(origin.z - APPLE_SEARCH_RADIUS),
    }
    local maxp = {
        x = math.floor(origin.x + APPLE_SEARCH_RADIUS),
        y = math.floor(origin.y + 16),
        z = math.floor(origin.z + APPLE_SEARCH_RADIUS),
    }
    local positions = minetest.find_nodes_in_area(minp, maxp, {"group:apple"})
    local nearest = nil
    local nearest_distance = nil
    for _, apple_pos in ipairs(positions or {}) do
        local node = minetest.get_node_or_nil(apple_pos)
        if node and is_tree_apple_node(node.name) then
            local approach = find_tree_apple_approach(apple_pos, origin)
            if approach then
                local distance = vector.distance(origin, approach)
                if not nearest_distance or distance < nearest_distance then
                    nearest = {
                        position = {
                            x = apple_pos.x,
                            y = apple_pos.y,
                            z = apple_pos.z,
                        },
                        approach = approach,
                        name = node.name,
                    }
                    nearest_distance = distance
                end
            end
        end
    end
    return nearest
end
local function harvest_tree_apple()
    if not target_tree_apple or not ani_object or not ani_object:get_pos() then
        return false
    end
    local apple_pos = target_tree_apple.position
    local node = minetest.get_node_or_nil(apple_pos)
    if not node or not is_tree_apple_node(node.name) then return false end
    local current = ani_object:get_pos()
    if vector.distance(current, apple_pos) > APPLE_REACH then return false end
    local look_delta = {
        x = apple_pos.x - current.x,
        y = 0,
        z = apple_pos.z - current.z,
    }
    if math.abs(look_delta.x) + math.abs(look_delta.z) > 0.05 then
        ani_object:set_yaw(minetest.dir_to_yaw(look_delta))
    end
    stop_autonomous_motion()
    local drops = minetest.get_node_drops(node.name, "")
    minetest.remove_node(apple_pos)
    local drop_name = drops and drops[1] or "rp_default:apple"
    local item = minetest.add_item({
        x = current.x,
        y = current.y + 0.2,
        z = current.z,
    }, drop_name)
    if item then item:set_velocity(vector.zero()) end
    memory.remember("apple_harvested", {
        item = drop_name,
        source = "tree",
        look_up = true,
        position = apple_pos,
    })
    emit_world_event("food_harvested", {
        item = drop_name,
        source = "tree",
        look_up = true,
        position = apple_pos,
    })
    minetest.log("action", string.format(
        "[anima_bridge] Ani looked up and harvested %s at (%d,%d,%d)",
        drop_name, apple_pos.x, apple_pos.y, apple_pos.z))
    target_tree_apple = nil
    clear_path()
    autonomy_elapsed = 0.0
    return true
end
is_safe_exploration_cell = function(pos)
    local floor_node = minetest.get_node_or_nil({x = pos.x, y = pos.y - 1, z = pos.z})
    local feet_node = minetest.get_node_or_nil(pos)
    local head_node = minetest.get_node_or_nil({x = pos.x, y = pos.y + 1, z = pos.z})
    if not floor_node or not feet_node or not head_node then return false end
    local floor_def = minetest.registered_nodes[floor_node.name]
    if not floor_def or floor_def.walkable ~= true then return false end
    if (floor_def.damage_per_second or 0) > 0 then return false end
    if floor_def.liquidtype and floor_def.liquidtype ~= "none" then return false end
    return not is_walkable(pos) and not is_walkable({x = pos.x, y = pos.y + 1, z = pos.z})
end
local function find_exploration_target()
    if not ani_object or not ani_object:get_pos() then return nil end
    local origin = vector.round(ani_object:get_pos())
    for _ = 1, 16 do
        local angle = math.random() * math.pi * 2
        local radius = math.random(EXPLORE_RADIUS_MIN, EXPLORE_RADIUS_MAX)
        local x = math.floor(origin.x + math.cos(angle) * radius + 0.5)
        local z = math.floor(origin.z + math.sin(angle) * radius + 0.5)
        for y = origin.y + 3, origin.y - 3, -1 do
            local candidate = {x = x, y = y, z = z}
            if vector.distance(origin, candidate) >= 3.0
                    and is_safe_exploration_cell(candidate) then
                return candidate
            end
        end
    end
    return nil
end
stop_autonomous_motion = function()
    if not ani_object or not ani_object:get_pos() then return end
    jump_active = false
    jump_elapsed = 0.0
    local velocity = ani_object:get_velocity() or vector.zero()
    velocity.x = 0
    velocity.z = 0
    ani_object:set_velocity(velocity)
    ani_object:set_acceleration({x = 0, y = JUMP_GRAVITY, z = 0})
    ani_object:set_animation({x = 0, y = 79}, 33, 0, true)
end
clear_path = function()
    path = nil
    path_index = 1
    path_last_pos = nil
    path_stuck_elapsed = 0.0
    path_target_label = nil
    path_goal = nil
    path_revision = nil
end
local function plan_path_to(goal, label)
    if not goal then
        clear_path()
        return false
    end
    local start = vector.round(ani_object:get_pos())
    goal = vector.round(goal)
    local goal_distance = vector.distance(start, goal)
    if goal_distance < 0.5 then
        clear_path()
        return true
    end

    -- rp_pathfinder ищет только в локальном радиусе. Для дальнего дерева,
    -- игрока или укрытия строим маршрут через ближайшую безопасную точку.
    if goal_distance > PATH_SEARCH_DISTANCE - 3 then
        local dx = (goal.x - start.x) / goal_distance
        local dz = (goal.z - start.z) / goal_distance
        local intermediate = nil
        local max_step = math.min(PATH_SEARCH_DISTANCE - 5, goal_distance - 1)
        for step = math.floor(max_step), 5, -1 do
            local x = math.floor(start.x + dx * step + 0.5)
            local z = math.floor(start.z + dz * step + 0.5)
            for dy = 1, -2, -1 do
                local candidate = {x = x, y = start.y + dy, z = z}
                if is_safe_exploration_cell(candidate) then
                    intermediate = candidate
                    break
                end
            end
            if intermediate then break end
        end
        if intermediate then
            goal = intermediate
            minetest.log("action", string.format(
                "[anima_bridge] path step toward %s: (%d,%d,%d)",
                tostring(label), goal.x, goal.y, goal.z))
        end
    end
    if not rp_pathfinder or not rp_pathfinder.find_path then
        clear_path()
        memory.remember("path_unavailable", {target = label})
        return false
    end
    local found, reason = rp_pathfinder.find_path(start, goal, PATH_SEARCH_DISTANCE, {
        max_jump = 1,
        max_drop = 1,
        clear_height = 2,
        respect_disable_jump = true,
    }, PATH_TIMEOUT)
    if not found then
        clear_path()
        memory.remember("path_failed", {target = label, reason = reason})
        minetest.log("action", "[anima_bridge] no path to " .. tostring(label) .. ": " .. tostring(reason))
        if minetest.get_gametime() - last_path_failure_at >= 5 then
            last_path_failure_at = minetest.get_gametime()
            emit_world_event("navigation_result", {success = false, reason = tostring(reason), target = goal})
        end
        return false
    end
    path = found
    path_index = 2
    path_target_label = label
    path_goal = goal
    path_revision = development_revision
    local current = ani_object:get_pos()
    path_last_pos = {x = current.x, y = current.y, z = current.z}
    path_stuck_elapsed = 0.0
    memory.remember("path_planned", {target = label, length = #path})
    return true
end
local function plan_food_path()
    if not target_food or not target_food:get_pos() then
        clear_path()
        return false
    end
    return plan_path_to(target_food:get_pos(), target_food_name)
end
local function plan_tree_apple_path()
    if not target_tree_apple or not target_tree_apple.approach then
        clear_path()
        return false
    end
    return plan_path_to(target_tree_apple.approach, "яблоко на дереве")
end
local function plan_exploration_path()
    if not exploration_target then
        clear_path()
        return false
    end
    return plan_path_to(exploration_target, "исследование")
end

local function refresh_social_target()
    if not social_player_name or not ani_object or not ani_object:get_pos() then
        return
    end
    for _, object in ipairs(minetest.get_objects_inside_radius(
            ani_object:get_pos(), OBJECT_OBSERVATION_RADIUS)) do
        if object:is_player()
                and object:get_player_name() == social_player_name then
            local pos = object:get_pos()
            if pos then
                social_target = {
                    x = math.floor(pos.x + 0.5),
                    y = math.floor(pos.y + 0.5),
                    z = math.floor(pos.z + 0.5),
                }
            end
            return
        end
    end
end

local function finish_social_mode()
    local restore_building = social_return_building
    local restore_exploration = social_return_exploration
    social_target = nil
    social_player_name = nil
    social_time_left = 0.0
    clear_path()
    stop_autonomous_motion()
    building_enabled = restore_building
    exploration_enabled = restore_exploration
    if restore_building then
        exploration_enabled = false
    end
    autonomy_elapsed = 1.0
    minetest.log("action", "[anima_bridge] human contact period ended; resuming previous goal")
end

-- Объявление вперёд: социальный и защитный режимы используют общий
-- пошаговый следопыт, который объявлен ниже по файлу.
local follow_food_path

local function run_social_mode(dtime)
    if not social_target or not ani_object or not ani_object:get_pos() then
        finish_social_mode()
        return
    end
    social_time_left = social_time_left - dtime
    refresh_social_target()
    local current = ani_object:get_pos()
    local delta = {
        x = social_target.x - current.x,
        y = 0,
        z = social_target.z - current.z,
    }
    local distance = math.sqrt(delta.x * delta.x + delta.z * delta.z)
    if distance <= SOCIAL_HOLD_DISTANCE then
        stop_autonomous_motion()
        clear_path()
        if distance > 0.05 then
            ani_object:set_yaw(minetest.dir_to_yaw(delta))
        end
        if social_time_left <= 0.0 then
            finish_social_mode()
        end
        return
    end
    if social_time_left <= 0.0 then
        finish_social_mode()
        return
    end
    if autonomy_elapsed >= 0.5 or not path then
        autonomy_elapsed = 0.0
        if not plan_path_to(social_target, "к человеку") then
            social_time_left = math.min(social_time_left, 0.5)
            stop_autonomous_motion()
            return
        end
    end
    follow_food_path(dtime)
end

local function get_home_entry()
    if not home_site then return nil end
    local dx, dz = development.door_offset(home_spec, -1)
    return {
        x = home_site.x + dx,
        y = home_site.y,
        -- Цель находится на один блок внутри дверного проёма,
        -- чтобы Aya действительно заходила под крышу.
        z = home_site.z + dz,
    }
end

local function is_inside_home(pos)
    if not home_site or not pos then return false end
    if math.abs(pos.x - home_site.x) > home_spec.half_size - 0.35
            or math.abs(pos.z - home_site.z) > home_spec.half_size - 0.35
            or pos.y < home_site.y - 0.5
            or pos.y > home_site.y + home_spec.wall_height + 0.5 then
        return false
    end
    local roof = minetest.get_node_or_nil({
        x = math.floor(pos.x + 0.5),
        y = home_site.y + home_spec.wall_height,
        z = math.floor(pos.z + 0.5),
    })
    return roof and roof.name ~= "air"
end

local function nearest_player_info(origin)
    local nearest = nil
    local nearest_pos = nil
    local nearest_distance = nil
    for _, player in ipairs(minetest.get_connected_players()) do
        local pos = player:get_pos()
        if pos then
            local distance = vector.distance(origin, pos)
            if not nearest_distance or distance < nearest_distance then
                nearest = player
                nearest_pos = pos
                nearest_distance = distance
            end
        end
    end
    return nearest, nearest_pos, nearest_distance
end

local function current_weather_is_storm()
    return type(weather) == "table"
        and type(weather.get_weather) == "function"
        and weather.get_weather() == "storm"
end

local function update_safety_state(dtime)
    safety_elapsed = safety_elapsed + dtime
    if not ani_object or not ani_object:get_pos() then
        return {mode = nil, night = false, storm = false, sheltered = false}
    end
    local pos = ani_object:get_pos()
    local timeofday = minetest.get_timeofday()
    local night = timeofday < NIGHT_START or timeofday > NIGHT_END
    local storm = current_weather_is_storm()
    local sheltered = is_inside_home(pos)
    local player, player_pos, player_distance = nearest_player_info(pos)
    local near_player = player_distance and player_distance <= SAFETY_PLAYER_RADIUS or false
    local touching_player = player_distance and player_distance <= TOUCH_PLAYER_RADIUS or false
    local mode = nil
    local target = nil
    local home_entry = get_home_entry()
    local home_distance = home_entry and vector.distance(pos, home_entry) or math.huge

    -- В шторм приоритетом является готовое укрытие. Ночью выбирается
    -- ближайшее безопасное место: дом или человек.
    if storm and not sheltered then
        if home_entry then
            mode = "shelter"
            target = home_entry
        elseif player and not near_player then
            mode = "player"
            target = player_pos
        end
    elseif night and not sheltered and not near_player then
        if home_entry and (not player or home_distance <= player_distance) then
            mode = "shelter"
            target = home_entry
        elseif player then
            mode = "player"
            target = player_pos
        elseif home_entry then
            mode = "shelter"
            target = home_entry
        end
    end

    safety_mode = mode
    safety_target = target
    local protected = sheltered or (near_player and not storm)
    local key = table.concat({
        night and "night" or "day",
        storm and "storm" or "clear",
        sheltered and "home" or "outside",
        near_player and "near" or "alone",
        touching_player and "touch" or "apart",
        mode or "free",
    }, ":")
    if key ~= safety_last_key or safety_elapsed >= SAFETY_SAMPLE_INTERVAL then
        safety_elapsed = 0.0
        safety_last_key = key
        emit_world_event("safety_state", {
            night = night,
            storm = storm,
            sheltered = sheltered,
            near_player = near_player,
            touching_player = touching_player,
            protected = protected,
            mode = mode,
            position = {
                x = pos.x,
                y = pos.y,
                z = pos.z,
            },
        })
        minetest.log("action", string.format(
            "[anima_bridge] safety night=%s storm=%s sheltered=%s near_player=%s mode=%s",
            tostring(night), tostring(storm), tostring(sheltered),
            tostring(near_player), tostring(mode)))
    end
    return {
        mode = mode,
        night = night,
        storm = storm,
        sheltered = sheltered,
        near_player = near_player,
        touching_player = touching_player,
        protected = protected,
    }
end

local function run_safety_mode(state, dtime)
    if not state or not state.mode or not ani_object or not ani_object:get_pos() then
        return false
    end
    local current = ani_object:get_pos()
    local target = safety_target
    if state.mode == "player" then
        local _, player_pos = nearest_player_info(current)
        target = player_pos
        safety_target = target
    end
    if not target then
        stop_autonomous_motion()
        clear_path()
        return true
    end
    if (state.mode == "shelter" and is_inside_home(current))
            or (state.mode == "player"
                and vector.distance(current, target) <= SOCIAL_HOLD_DISTANCE) then
        stop_autonomous_motion()
        clear_path()
        return true
    end
    if autonomy_elapsed >= 0.5 or not path then
        autonomy_elapsed = 0.0
        plan_path_to(target, state.mode == "shelter" and "укрытие" or "к игроку")
    end
    follow_food_path(dtime)
    return true
end

local function build_space_is_empty(pos)
    local node = minetest.get_node_or_nil(pos)
    if not node then return false end
    if node.name == "air" then return true end
    local definition = minetest.registered_nodes[node.name]
    return definition and definition.buildable_to == true
end

local function build_site_is_suitable(site)
    for _, failed in ipairs(failed_build_sites) do
        if vector.distance(site, failed.position) < 4 and minetest.get_gametime() - failed.time < 300 then
            return false
        end
    end
    local dx, dz = development.door_offset(build_spec, 1)
    if not is_safe_exploration_cell({x = site.x + dx, y = site.y, z = site.z + dz}) then
        return false
    end
    for dx = -BUILD_HALF_SIZE, BUILD_HALF_SIZE do
        for dz = -BUILD_HALF_SIZE, BUILD_HALF_SIZE do
            local cell = {x = site.x + dx, y = site.y, z = site.z + dz}
            if not is_safe_exploration_cell(cell) then return false end
            for dy = 0, BUILD_ROOF_LEVEL do
                if not build_space_is_empty({x = cell.x, y = cell.y + dy, z = cell.z}) then
                    return false
                end
            end
        end
    end
    return true
end

local function find_build_site()
    if not ani_object or not ani_object:get_pos() then return nil end
    local origin = vector.round(ani_object:get_pos())
    for radius = 4, 14 do
        for dx = -radius, radius do
            for dz = -radius, radius do
                if math.max(math.abs(dx), math.abs(dz)) == radius then
                    for dy = 2, -2, -1 do
                        local site = {
                            x = origin.x + dx,
                            y = origin.y + dy,
                            z = origin.z + dz,
                        }
                        if build_site_is_suitable(site) then return site end
                    end
                end
            end
        end
    end
    return nil
end

local function make_build_plan(site)
    return development.blueprint(site, build_spec)
end
local function find_build_resource_approach(resource_pos, origin)
    local best = nil
    local best_distance = nil
    for dx = -2, 2 do
        for dz = -2, 2 do
            for y = resource_pos.y + 1, resource_pos.y - 4, -1 do
                local candidate = {
                    x = resource_pos.x + dx,
                    y = y,
                    z = resource_pos.z + dz,
                }
                local horizontal_offset = math.abs(dx) + math.abs(dz)
                if horizontal_offset > 0
                        and vector.distance(candidate, resource_pos) <= BUILD_RESOURCE_REACH
                        and is_safe_exploration_cell(candidate) then
                    local distance = vector.distance(origin, candidate)
                    if not best_distance or distance < best_distance then
                        best = candidate
                        best_distance = distance
                    end
                end
            end
        end
    end
    return best
end

local function same_block(a, b)
    return a and b
        and math.floor(a.x + 0.5) == math.floor(b.x + 0.5)
        and math.floor(a.y + 0.5) == math.floor(b.y + 0.5)
        and math.floor(a.z + 0.5) == math.floor(b.z + 0.5)
end

local function remember_build_resource(resource)
    if not resource or not resource.position or not resource.name then return end
    for _, known in ipairs(build_resource_memory) do
        if same_block(known.position, resource.position) then
            known.name = resource.name
            known.approach = resource.approach
            return
        end
    end
    table.insert(build_resource_memory, {
        position = {
            x = resource.position.x,
            y = resource.position.y,
            z = resource.position.z,
        },
        approach = resource.approach,
        name = resource.name,
    })
    while #build_resource_memory > BUILD_RESOURCE_MEMORY_LIMIT do
        table.remove(build_resource_memory, 1)
    end
    storage:set_string("development.resources", minetest.serialize(build_resource_memory))
    memory.remember("build_resource_seen", {
        position = resource.position,
        name = resource.name,
    })
end

local function forget_build_resource(resource)
    if not resource or not resource.position then return end
    for index = #build_resource_memory, 1, -1 do
        if same_block(build_resource_memory[index].position, resource.position) then
            table.remove(build_resource_memory, index)
        end
    end
    storage:set_string("development.resources", minetest.serialize(build_resource_memory))
end

local function find_known_build_resource(origin)
    local nearest = nil
    local nearest_distance = nil
    for index = #build_resource_memory, 1, -1 do
        local known = build_resource_memory[index]
        local node = minetest.get_node_or_nil(known.position)
        if node and node.name ~= known.name then
            emit_world_event("resource_missing", {item = known.name, position = known.position})
            table.remove(build_resource_memory, index)
        elseif node then
            local approach = find_build_resource_approach(known.position, origin)
            if approach then
                local distance = vector.distance(origin, approach)
                if not nearest_distance or distance < nearest_distance then
                    nearest = {
                        position = {
                            x = known.position.x,
                            y = known.position.y,
                            z = known.position.z,
                        },
                        approach = approach,
                        name = known.name,
                    }
                    nearest_distance = distance
                end
            end
        end
    end
    return nearest
end

local function find_nearest_build_resource()
    if not ani_object or not ani_object:get_pos() then return nil end
    local origin = ani_object:get_pos()
    local remembered = prefer_resource_memory and find_known_build_resource(origin)
    if remembered then
        return remembered
    end
    local names = build_log_name and {build_log_name} or BUILD_TREE_NAMES
    local minp = {
        x = math.floor(origin.x - BUILD_RESOURCE_RADIUS),
        y = math.floor(origin.y - 8),
        z = math.floor(origin.z - BUILD_RESOURCE_RADIUS),
    }
    local maxp = {
        x = math.floor(origin.x + BUILD_RESOURCE_RADIUS),
        y = math.floor(origin.y + 8),
        z = math.floor(origin.z + BUILD_RESOURCE_RADIUS),
    }
    local positions = minetest.find_nodes_in_area(minp, maxp, names)
    local nearest = nil
    local nearest_distance = nil
    for _, resource_pos in ipairs(positions or {}) do
        local node = minetest.get_node_or_nil(resource_pos)
        if node and BUILD_PLANKS_BY_TREE[node.name] then
            local approach = find_build_resource_approach(resource_pos, origin)
            if approach then
                local distance = vector.distance(origin, approach)
                if not nearest_distance or distance < nearest_distance then
                    nearest = {
                        position = {
                            x = resource_pos.x,
                            y = resource_pos.y,
                            z = resource_pos.z,
                        },
                        approach = approach,
                        name = node.name,
                    }
                    nearest_distance = distance
                end
            end
        end
    end
    if nearest and not build_log_name then
        build_log_name = nearest.name
        build_material = BUILD_PLANKS_BY_TREE[nearest.name]
    end
    if nearest then
        remember_build_resource(nearest)
    end
    return nearest
end

local function collect_build_resource()
    if not build_resource_target or not ani_object or not ani_object:get_pos() then
        return false
    end
    local resource_pos = build_resource_target.position
    local node = minetest.get_node_or_nil(resource_pos)
    if not node or node.name ~= build_resource_target.name then
        build_resource_target = nil
        clear_path()
        return false
    end
    if vector.distance(ani_object:get_pos(), resource_pos) > BUILD_RESOURCE_REACH then
        return false
    end
    stop_autonomous_motion()
    -- Убираем весь вертикальный ствол за один подход: после удаления
    -- нижнего блока верхние блоки больше не должны оставаться недоступными.
    local harvested = 0
    for dy = -8, 8 do
        local tree_pos = {
            x = resource_pos.x,
            y = resource_pos.y + dy,
            z = resource_pos.z,
        }
        local tree_node = minetest.get_node_or_nil(tree_pos)
        if tree_node and tree_node.name == build_resource_target.name then
            minetest.remove_node(tree_pos)
            harvested = harvested + 1
        end
    end
    if harvested <= 0 then
        build_resource_target = nil
        clear_path()
        return false
    end
    build_inventory.logs = build_inventory.logs + harvested
    build_inventory.planks = build_inventory.planks + harvested * 4
    memory.remember("resource_gathered", {
        item = node.name,
        planks = build_material,
        blocks = harvested,
        total_planks = build_inventory.planks,
    })
    emit_world_event("resource_gathered", {
        position = resource_pos,
        item = node.name,
        output = build_material,
        amount = harvested * 4,
        blocks = harvested,
        total_planks = build_inventory.planks,
    })
    minetest.log("action", string.format(
        "[anima_bridge] Aya gathered %d %s block(s) -> %d %s (planks=%d)",
        harvested, node.name, harvested * 4, build_material, build_inventory.planks))
    forget_build_resource(build_resource_target)
    build_resource_target = nil
    clear_path()
    autonomy_elapsed = 0.0
    return true
end

follow_food_path = function(dtime)
    if not path or not path[path_index] then
        if path and path_goal and ani_object and ani_object:get_pos() then
            emit_world_event("navigation_result", {
                success = vector.distance(ani_object:get_pos(), path_goal) < 1.6,
                target = path_goal, position = ani_object:get_pos(),
                development_revision = path_revision,
            })
        end
        -- Маршрут закончился: очистить его, чтобы цель была рассчитана заново.
        clear_path()
        stop_autonomous_motion()
        return
    end
    local current = ani_object:get_pos()
    if not path_last_pos then
        path_last_pos = {x = current.x, y = current.y, z = current.z}
    elseif vector.distance(current, path_last_pos) >= 0.08 then
        path_last_pos = {x = current.x, y = current.y, z = current.z}
        path_stuck_elapsed = 0.0
    else
        path_stuck_elapsed = path_stuck_elapsed + dtime
    end
    if path_stuck_elapsed >= 1.5 then
        emit_world_event("navigation_result", {success = false, reason = "stuck", target = path_goal,
            position = current, development_revision = path_revision})
        path = nil
        path_index = 1
        path_last_pos = nil
        path_stuck_elapsed = 0.0
        autonomy_elapsed = 1.0
        stop_autonomous_motion()
        memory.remember("path_replanned", {target = path_target_label, reason = "obstacle"})
        minetest.log("action", "[anima_bridge] Ani stopped at obstacle; replanning")
        emit_world_event("obstacle", {target = path_target_label})
        return
    end
    jump_cooldown = math.max(0.0, jump_cooldown - dtime)
    local velocity = ani_object:get_velocity() or vector.zero()
    local moveresult = last_moveresult
    local touching_ground = moveresult and moveresult.touching_ground
    local wall_blocked = collides_with_node_wall(moveresult)
    if jump_active then
        jump_elapsed = jump_elapsed + dtime
        if touching_ground and jump_elapsed >= 0.2 and velocity.y <= 0.1 then
            jump_active = false
            jump_elapsed = 0.0
            velocity.y = 0
        elseif jump_elapsed >= MAX_JUMP_TIME then
            if not touching_ground then
                minetest.log("action", "[anima_bridge] Ani jump timeout; preserving falling physics")
            end
            jump_active = false
            jump_elapsed = 0.0
        end
    end
    local waypoint = path[path_index]
    local delta = {
        x = waypoint.x - current.x,
        y = 0,
        z = waypoint.z - current.z,
    }
    local distance = math.sqrt(delta.x * delta.x + delta.z * delta.z)
    if distance < 0.35 then
        path_index = path_index + 1
        return
    end
    local speed = walk_speed
    ani_object:set_yaw(minetest.dir_to_yaw({x = delta.x, y = 0, z = delta.z}))
    if waypoint.y > current.y + 0.35 and wall_blocked
            and not jump_active and jump_cooldown <= 0.0 and touching_ground then
        jump_active = true
        jump_origin = {x = current.x, y = current.y, z = current.z}
        jump_revision = development_revision
        jump_observed_elapsed = 0
        jump_elapsed = 0.0
        jump_cooldown = JUMP_COOLDOWN
        velocity.y = JUMP_VELOCITY
        memory.remember("jump", {target = path_target_label})
        minetest.log("action", "[anima_bridge] Ani jumps toward " .. tostring(path_target_label))
    end
    velocity.x = delta.x / distance * speed
    velocity.z = delta.z / distance * speed
    if jump_active then
        ani_object:set_acceleration({x = 0, y = JUMP_GRAVITY, z = 0})
    else
        if touching_ground then
            velocity.y = 0
        end
        ani_object:set_acceleration({x = 0, y = JUMP_GRAVITY, z = 0})
    end
    ani_object:set_velocity(velocity)
    ani_object:set_animation({x = 168, y = 187}, 33, 0, true)
end

local function node_is_liquid(pos)
    local node = minetest.get_node_or_nil(pos)
    if not node then return false end
    local definition = minetest.registered_nodes[node.name]
    return definition and definition.liquidtype and definition.liquidtype ~= "none"
end

local function ani_is_in_liquid(pos)
    if not pos then return false end
    local cell = vector.round(pos)
    return node_is_liquid(cell)
        or node_is_liquid({x = cell.x, y = cell.y + 1, z = cell.z})
end

local function find_nearest_land(origin)
    local start = vector.round(origin)
    for radius = 1, 8 do
        for dx = -radius, radius do
            for dz = -radius, radius do
                if math.max(math.abs(dx), math.abs(dz)) == radius then
                    for dy = 2, -3, -1 do
                        local candidate = {
                            x = start.x + dx,
                            y = start.y + dy,
                            z = start.z + dz,
                        }
                        if is_safe_exploration_cell(candidate) then
                            return candidate
                        end
                    end
                end
            end
        end
    end
    return nil
end

local function run_liquid_escape(dtime)
    if not ani_object or not ani_object:get_pos() then return false end
    local current = ani_object:get_pos()
    if not ani_is_in_liquid(current) then
        if liquid_escape_target then
            minetest.log("action", "[anima_bridge] Aya reached dry land")
        end
        liquid_escape_target = nil
        liquid_escape_elapsed = 0.0
        if path_target_label == "берег" then clear_path() end
        return false
    end

    liquid_escape_elapsed = liquid_escape_elapsed + dtime
    if not liquid_escape_target or liquid_escape_elapsed >= 0.5 then
        liquid_escape_elapsed = 0.0
        liquid_escape_target = find_nearest_land(current)
        clear_path()
        if liquid_escape_target then
            memory.remember("liquid_escape", {target = liquid_escape_target})
            minetest.log("action", string.format(
                "[anima_bridge] Aya ищет берег: (%d,%d,%d)",
                liquid_escape_target.x, liquid_escape_target.y, liquid_escape_target.z))
        end
    end

    if liquid_escape_target and (not path or path_target_label ~= "берег") then
        plan_path_to(liquid_escape_target, "берег")
    end
    if path and path_target_label == "берег" then
        follow_food_path(dtime)
        return true
    end

    -- Запасной выход, если pathfinder не умеет строить путь из воды.
    local velocity = ani_object:get_velocity() or vector.zero()
    if liquid_escape_target then
        local delta = {
            x = liquid_escape_target.x - current.x,
            y = 0,
            z = liquid_escape_target.z - current.z,
        }
        local distance = math.sqrt(delta.x * delta.x + delta.z * delta.z)
        if distance > 0.1 then
            ani_object:set_yaw(minetest.dir_to_yaw(delta))
            velocity.x = delta.x / distance * 1.2
            velocity.z = delta.z / distance * 1.2
        end
    else
        velocity.x = 0
        velocity.z = 0
    end
    velocity.y = math.max(velocity.y, 2.0)
    ani_object:set_velocity(velocity)
    ani_object:set_acceleration({x = 0, y = -2.0, z = 0})
    ani_object:set_animation({x = 168, y = 187}, 33, 0, true)
    return true
end

local function finish_building(success, reason)
    local quality = nil
    if success then
        local function known_solid(pos)
            local node = minetest.get_node_or_nil(pos)
            return node ~= nil and is_walkable(pos) == true
        end
        success, quality = development.inspect(build_site, build_spec, known_solid, build_space_is_empty)
        if not success then reason = "дом не прошёл проверку опоры, прохода или внутреннего пространства" end
    end
    if not success and build_site then
        table.insert(failed_build_sites, {position = build_site, time = minetest.get_gametime()})
        if #failed_build_sites > 20 then table.remove(failed_build_sites, 1) end
    end
    local label = success and "build_completed" or "build_failed"
    memory.remember(label, {
        reason = reason,
        planks = build_inventory.planks,
        logs = build_inventory.logs,
        site = build_site,
    })
    emit_world_event(label, {
        quality = quality,
        blueprint = build_spec,
        reason = reason,
        planks = build_inventory.planks,
        logs = build_inventory.logs,
        site = build_site,
    })
    minetest.log("action", "[anima_bridge] " .. (success
        and "Aya finished an autonomous shelter"
        or ("Aya stopped building: " .. tostring(reason))))
    building_enabled = false
    build_state = success and "complete" or "failed"
    if success and build_site then
        home_site = {
            x = build_site.x,
            y = build_site.y,
            z = build_site.z,
            spec = {half_size = build_spec.half_size, wall_height = build_spec.wall_height, door = build_spec.door},
        }
        home_spec = home_site.spec
        storage:set_string("home_site", minetest.serialize(home_site))
    end
    build_resource_target = nil
    build_search_target = nil
    build_search_elapsed = 0.0
    clear_path()
    stop_autonomous_motion()
    exploration_enabled = true
    autonomy_elapsed = 0.0
end

local function run_building(dtime)
    if not building_enabled or not ani_object or not ani_object:get_pos() then return end
    build_cooldown = math.max(0.0, build_cooldown - dtime)
    if build_state == "choose_site" then
        if build_cooldown > 0.0 then return end
        build_site = find_build_site()
        if not build_site then
            build_cooldown = 5.0
            minetest.log("action", "[anima_bridge] Aya cannot find a safe building site yet")
            return
        end
        build_plan = make_build_plan(build_site)
        BUILD_PLANKS_NEEDED = #build_plan
        build_index = 1
        build_state = "gather"
        memory.remember("build_site_chosen", {
            site = build_site,
            plan = #build_plan,
            structure = build_spec,
        })
        emit_world_event("build_started", {site = build_site, plan = #build_plan})
        minetest.log("action", string.format(
            "[anima_bridge] Aya chose building site=(%d,%d,%d), plan=%d blocks",
            build_site.x, build_site.y, build_site.z, #build_plan))
        return
    end

    if build_state == "gather" then
        if build_inventory.planks >= BUILD_PLANKS_NEEDED then
            build_state = "build"
            build_index = 1
            clear_path()
            autonomy_elapsed = 0.0
            minetest.log("action", string.format(
                "[anima_bridge] Aya has enough material: %d planks; building starts",
                build_inventory.planks))
            return
        end
        if not build_resource_target then
            if build_cooldown <= 0.0 then
                build_resource_target = find_nearest_build_resource()
            end
            if build_resource_target then
                build_search_target = nil
                build_search_elapsed = 0.0
                clear_path()
                minetest.log("action", string.format(
                    "[anima_bridge] Aya found tree for building at (%d,%d,%d), approach=(%d,%d,%d)",
                    build_resource_target.position.x,
                    build_resource_target.position.y,
                    build_resource_target.position.z,
                    build_resource_target.approach.x,
                    build_resource_target.approach.y,
                    build_resource_target.approach.z))
            else
                if build_cooldown <= 0.0 then
                    build_cooldown = 5.0
                    minetest.log("action", "[anima_bridge] Aya found no suitable tree nearby; searching the world")
                end
                build_search_elapsed = build_search_elapsed + dtime
                local search_distance = build_search_target
                    and vector.distance(ani_object:get_pos(), build_search_target)
                    or math.huge
                if not build_search_target
                        or search_distance <= 1.5
                        or build_search_elapsed >= 20.0 then
                    build_search_target = find_exploration_target()
                    build_search_elapsed = 0.0
                    clear_path()
                    if build_search_target then
                        memory.remember("build_search", {target = build_search_target})
                        minetest.log("action", string.format(
                            "[anima_bridge] Aya searches for tree at (%d,%d,%d)",
                            build_search_target.x,
                            build_search_target.y,
                            build_search_target.z))
                    end
                end
                if build_search_target then
                    if autonomy_elapsed >= 1.0 or not path then
                        autonomy_elapsed = 0.0
                        if not plan_path_to(build_search_target, "поиск дерева") then
                            build_search_target = nil
                            clear_path()
                        end
                    end
                    if build_search_target and path then
                        follow_food_path(dtime)
                    end
                else
                    stop_autonomous_motion()
                end
                return
            end
        end
        local resource_pos = build_resource_target.position
        local node = minetest.get_node_or_nil(resource_pos)
        if not node or node.name ~= build_resource_target.name then
            build_resource_target = nil
            clear_path()
            return
        end
        local distance = vector.distance(ani_object:get_pos(), resource_pos)
        if distance <= BUILD_RESOURCE_REACH then
            collect_build_resource()
        else
            if autonomy_elapsed >= 1.0 or (not path and autonomy_elapsed >= 0.5) then
                autonomy_elapsed = 0.0
                if not plan_path_to(build_resource_target.approach, "добыча древесины") then
                    -- Не забываем дерево из-за временного тайм-аута поиска:
                    -- маршрут будет повторён после короткой паузы.
                    clear_path()
                end
            end
            if build_resource_target then follow_food_path(dtime) end
        end
        return
    end

    if build_state == "build" then
        if build_index > #build_plan then
            finish_building(true, "план выполнен")
            return
        end
        if build_cooldown > 0.0 then return end
        local target = build_plan[build_index]
        if not build_space_is_empty(target) then
            finish_building(false, "клетка занята")
            return
        end
        minetest.set_node(target, {name = build_material})
        build_inventory.planks = build_inventory.planks - 1
        memory.remember("construction_step", {
            position = target,
            material = build_material,
            remaining = build_inventory.planks,
        })
        emit_world_event("construction_step", {
            position = target,
            material = build_material,
            step = build_index,
            total = #build_plan,
        })
        minetest.log("action", string.format(
            "[anima_bridge] Aya placed %s at (%d,%d,%d) step=%d/%d",
            build_material, target.x, target.y, target.z,
            build_index, #build_plan))
        build_index = build_index + 1
        build_cooldown = 0.25
    end
end

local function find_spawn_pos(base)
    local offsets = {
        {x = 2, y = 0, z = 0},
        {x = -2, y = 0, z = 0},
        {x = 0, y = 0, z = 2},
        {x = 0, y = 0, z = -2},
        {x = 1, y = 0, z = 1},
        {x = -1, y = 0, z = 1},
        {x = 1, y = 0, z = -1},
        {x = -1, y = 0, z = -1},
    }
    local base_cell = {
        x = math.floor(base.x + 0.5),
        y = math.floor(base.y + 0.5),
        z = math.floor(base.z + 0.5),
    }
    for _, offset in ipairs(offsets) do
        local x = base_cell.x + offset.x
        local z = base_cell.z + offset.z
        local floor_pos = {x = x, y = base_cell.y - 1, z = z}
        local feet_pos = {x = x, y = base_cell.y, z = z}
        local head_pos = {x = x, y = base_cell.y + 1, z = z}
        if is_walkable(floor_pos) and not is_walkable(feet_pos) and not is_walkable(head_pos) then
            return feet_pos
        end
    end
    return {x = base_cell.x + 2, y = base_cell.y, z = base_cell.z}
end
minetest.register_entity(modname .. ":body", {
    initial_properties = {
        physical = true,
        collide_with_objects = true,
        collisionbox = {-0.3, 0, -0.3, 0.3, 1.77, 0.3},
        selectionbox = {-0.32, 0, -0.22, 0.32, 1.77, 0.22, rotate = true},
        stepheight = 0.626,
        visual = "mesh",
        mesh = "ani_character.b3d",
        textures = {"ani_female.png"},
        visual_size = {x = 1.0, y = 1.0},
        static_save = false,
    },
    on_step = function(self, dtime, moveresult)
        last_moveresult = moveresult
        -- Landing can happen after a path ends. Observe it independently of movement control.
        if jump_origin then
            jump_observed_elapsed = jump_observed_elapsed + dtime
            if moveresult and moveresult.touching_ground and jump_observed_elapsed >= 0.2 then
                local pos = self.object:get_pos()
                local rise = pos and pos.y - jump_origin.y or 0
                emit_world_event("jump_result", {success = rise > 0.4, rise = rise,
                    development_revision = jump_revision})
                jump_origin = nil
            elseif jump_observed_elapsed > 3 then
                emit_world_event("jump_result", {success = false, reason = "landing_timeout",
                    development_revision = jump_revision})
                jump_origin = nil
            end
        end
    end,
    on_activate = function(self)
        self.object:set_acceleration({x = 0, y = JUMP_GRAVITY, z = 0})
        self.object:set_animation({x = 0, y = 79}, 33, 0, true)
    end,
    on_punch = function(self, puncher, time_from_last_punch, tool_capabilities, dir, damage)
        if not damage or damage <= 0 then return end
        local source = puncher and puncher:get_player_name() or "неизвестный источник"
        emit_world_event("pain", {
            source = source,
            severity = math.min(1.0, math.max(0.1, (damage or 1) / 10)),
        })
    end,
})
minetest.register_chatcommand("anima_spawn", {
    description = "Создать тело Ani рядом с игроком",
    func = function(name)
        local player = minetest.get_player_by_name(name)
        if not player then return false, "Игрок не найден." end
        if ani_object and ani_object:get_pos() then return false, "Тело Ani уже существует." end
        local pos = find_spawn_pos(player:get_pos())
        ani_object = minetest.add_entity(pos, modname .. ":body")
        if not ani_object then return false, "Не удалось создать тело Ani." end
        ani_object:set_nametag_attributes({text = "Ani | AnimaOS", color = "#7de3d1"})
        autonomy_enabled = true
        exploration_enabled = true
        target_food = nil
        target_tree_apple = nil
        social_target = nil
        social_player_name = nil
        social_time_left = 0.0
        exploration_target = nil
        building_enabled = false
        build_state = "idle"
        build_site = nil
        build_plan = {}
        build_index = 1
        build_resource_target = nil
        build_inventory = {logs = 0, planks = 0}
        exploration_elapsed = 0.0
        clear_path()
        autonomy_elapsed = 1.0
        memory.remember("autonomy", {enabled = true, reason = "spawn"})
        minetest.log("action", string.format("[anima_bridge] body position=(%d, %d, %d)", pos.x, pos.y, pos.z))
        return true, "Тело Ani создано рядом с вами."
    end,
})
minetest.register_chatcommand("anima_build", {
    params = "<start|stop|status>",
    description = "Автономно добывать ресурсы и построить укрытие Aya",
    func = function(name, param)
        param = (param or ""):lower()
        if param == "start" then
            if not ani_object or not ani_object:get_pos() then
                return false, "Сначала выполните /anima_spawn."
            end
            autonomy_enabled = true
            exploration_enabled = false
            target_food = nil
            target_tree_apple = nil
            exploration_target = nil
            building_enabled = true
            build_state = "choose_site"
            build_site = nil
            build_plan = {}
            build_index = 1
            build_resource_target = nil
            build_search_target = nil
            build_search_elapsed = 0.0
            build_log_name = nil
            build_material = "rp_default:planks"
            build_inventory = {logs = 0, planks = 0}
            build_cooldown = 0.0
            clear_path()
            autonomy_elapsed = 1.0
            memory.remember("build_mode", {enabled = true, plan = "5x5 room with 3-block walls and an open doorway"})
            return true, "Aya начинает строить сама: ищет место, добывает дерево и возводит укрытие."
        elseif param == "stop" then
            building_enabled = false
            build_state = "idle"
            build_resource_target = nil
            build_search_target = nil
            build_search_elapsed = 0.0
            clear_path()
            stop_autonomous_motion()
            exploration_enabled = true
            memory.remember("build_mode", {enabled = false})
            return true, "Автономное строительство остановлено."
        elseif param == "status" then
            return true, string.format(
                "Строительство: %s, этап: %s, доски: %d/%d, брёвна: %d",
                building_enabled and "включено" or "выключено",
                build_state,
                build_inventory.planks,
                BUILD_PLANKS_NEEDED,
                build_inventory.logs)
        end
        return false, "Использование: /anima_build start|stop|status"
    end,
})
minetest.register_chatcommand("anima_status", {
    description = "Показать внутреннее состояние и текущий режим Aya",
    func = function()
        if not ani_object or not ani_object:get_pos() then
            return false, "Сначала выполните /anima_spawn."
        end
        local pos = ani_object:get_pos()
        local safety = update_safety_state(0)
        local home = home_site and "известен" or "не найден"
        return true, string.format(
            "Aya: позиция %.1f %.1f %.1f | режим=%s | строительство=%s/%s | дом=%s | ночь=%s дождь=%s рядом=%s",
            pos.x, pos.y, pos.z,
            safety_mode or "free",
            building_enabled and "on" or "off",
            build_state,
            home,
            tostring(safety.night),
            tostring(safety.storm),
            tostring(safety.near_player))
    end,
})
minetest.register_chatcommand("anima_reflect", {
    description = "Попросить Aya описать своё внутреннее состояние",
    func = function(name)
        if not ani_object or not ani_object:get_pos() then
            return false, "Сначала выполните /anima_spawn."
        end
        emit_world_event("reflection_request", {requester = name})
        return true, "Aya выполняет локальную саморефлексию. Результат появится в терминале моста."
    end,
})
minetest.register_chatcommand("anima_where", {
    description = "Показать координаты тела Ani",
    func = function(name)
        if not ani_object or not ani_object:get_pos() then return false, "Тело Ani ещё не создано." end
        local pos = ani_object:get_pos()
        return true, string.format("Ani находится: %.1f, %.1f, %.1f", pos.x, pos.y, pos.z)
    end,
})
minetest.register_chatcommand("anima_hunger", {
    description = "Показать голод и насыщение Ani",
    func = function()
        return true, string.format(
            "Ani: голод %d/%d, насыщение %d/%d",
            hunger, HUNGER_MAX, saturation, SATURATION_MAX)
    end,
})
minetest.register_chatcommand("anima_auto", {
    params = "<on|off>",
    description = "Включить или выключить автономное движение Ani",
    func = function(name, param)
        param = param:lower()
        if param == "on" then
            if not ani_object or not ani_object:get_pos() then
                return false, "Сначала выполните /anima_spawn."
            end
            autonomy_enabled = true
            exploration_enabled = true
            target_food = nil
            target_tree_apple = nil
            exploration_target = nil
            clear_path()
            autonomy_elapsed = 1.0
            memory.remember("autonomy", {enabled = true})
            return true, "Автономное движение включено. Ani ищет еду и изучает мир."
        elseif param == "off" then
            autonomy_enabled = false
            target_food = nil
            target_tree_apple = nil
            exploration_target = nil
            clear_path()
            stop_autonomous_motion()
            memory.remember("autonomy", {enabled = false})
            return true, "Автономное движение выключено."
        end
        return false, "Использование: /anima_auto on или /anima_auto off"
    end,
})
minetest.register_chatcommand("anima_explore", {
    params = "<on|off>",
    description = "Включить или выключить исследование мира Ani",
    func = function(name, param)
        param = param:lower()
        if param == "on" then
            if not ani_object or not ani_object:get_pos() then
                return false, "Сначала выполните /anima_spawn."
            end
            autonomy_enabled = true
            exploration_enabled = true
            exploration_target = nil
            clear_path()
            return true, "Исследование мира включено."
        elseif param == "off" then
            exploration_enabled = false
            exploration_target = nil
            if not target_food and not target_tree_apple then
                clear_path()
                stop_autonomous_motion()
            end
            return true, "Исследование мира выключено."
        end
        return false, "Использование: /anima_explore on или /anima_explore off"
    end,
})
minetest.register_chatcommand("anima_memory", {
    description = "Показать краткую информацию о памяти Ani",
    func = function()
        local last = memory.last()
        if not last then
            return true, string.format("Память Ani пока пуста. Клеток мира: %d.", memory.world_count())
        end
        return true, string.format(
            "Память: %d событий, клеток мира: %d, последнее: %s",
            memory.count(), memory.world_count(), tostring(last.kind))
    end,
})
minetest.register_chatcommand("anima_move", {
    params = "<north|south|east|west>",
    description = "Сделать тестовое движение тела Ani",
    func = function(name, param)
        if not ani_object or not ani_object:get_pos() then return false, "Сначала выполните /anima_spawn." end
        local direction = directions[param:lower()]
        if not direction then return false, "Направление: north, south, east или west." end
        move_token = move_token + 1
        local token = move_token
        local started = ani_object:get_pos()
        local elapsed = 0.0
        local function stop_motion()
            if ani_object and ani_object:get_pos() then
                ani_object:set_velocity(vector.zero())
                ani_object:set_acceleration(vector.zero())
                ani_object:set_animation({x = 0, y = 79}, 33, 0, true)
            end
        end
        ani_object:set_acceleration(vector.zero())
        ani_object:set_velocity(vector.multiply(direction, 2))
        ani_object:set_animation({x = 168, y = 187}, 33, 0, true)
        local function monitor()
            if token ~= move_token or not ani_object or not ani_object:get_pos() then return end
            local current = ani_object:get_pos()
            local moved = math.abs(current.x - started.x) + math.abs(current.z - started.z)
            elapsed = elapsed + 0.25
            if moved < 0.05 and elapsed >= 0.25 then
                stop_motion()
                if name then minetest.chat_send_player(name, "[AnimaOS] Препятствие впереди. Движение остановлено.") end
                minetest.log("action", "[anima_bridge] obstacle detected by physics")
                return
            end
            if elapsed >= 2.0 then
                stop_motion()
                return
            end
            minetest.after(0.25, monitor)
        end
        minetest.after(0.25, monitor)
        minetest.log("action", "[anima_bridge] move command: " .. param:lower())
        return true, "Ani движется: " .. param:lower()
    end,
})
local function run_registered_ai_command(name, param)
    local command = minetest.registered_chatcommands
        and minetest.registered_chatcommands[name]
    if not command or not command.func then
        return false, "command_unavailable"
    end
    local ok, message = command.func("singleplayer", param or "")
    return ok ~= false, message or "accepted"
end
local function emit_planner_result(action, success, result)
    emit_world_event("planner_command", {
        action = action,
        success = success,
        result = result,
    })
    minetest.log("action", string.format(
        "[anima_bridge] planner action=%s success=%s result=%s",
        action, tostring(success), tostring(result)))
end
local function apply_ai_command(command)
    if type(command) ~= "table" or type(command.action) ~= "string" then
        return
    end
    local action = command.action
    if action ~= "rest" then rest_elapsed = nil end
    local success = false
    local result = "rejected"
    if social_target and action ~= "socialize" then
        emit_planner_result(action, false, "human_contact_priority")
        return
    end
    if action == "socialize" then
        local target = command.target
        if type(target) == "table" and ani_object and ani_object:get_pos() then
            local tx, ty, tz = tonumber(target.x), tonumber(target.y), tonumber(target.z)
            if tx and ty and tz then
                if not social_target then
                    social_return_building = building_enabled
                    social_return_exploration = exploration_enabled
                end
                social_target = {
                    x = math.floor(tx + 0.5),
                    y = math.floor(ty + 0.5),
                    z = math.floor(tz + 0.5),
                }
                social_player_name = type(command.player) == "string"
                    and command.player or "singleplayer"
                social_time_left = math.max(
                    5.0, math.min(60.0, tonumber(command.duration) or SOCIAL_DURATION))
                clear_path()
                autonomy_enabled = true
                stop_autonomous_motion()
                success = true
                result = "human_contact_priority"
            else
                result = "invalid human target"
            end
        else
            result = "human target unavailable"
        end
    elseif action == "explore" then
        success, result = run_registered_ai_command("anima_explore", "on")
    elseif action == "seek_food" then
        success, result = run_registered_ai_command("anima_auto", "on")
    elseif action == "build" then
        if building_enabled then
            success, result = true, "build already active"
        else
            success, result = run_registered_ai_command("anima_build", "start")
        end
    elseif action == "rest" then
        local ok1, msg1 = run_registered_ai_command("anima_build", "stop")
        local ok2, msg2 = run_registered_ai_command("anima_auto", "off")
        success = ok1 and ok2
        result = msg2 or msg1
        if success then rest_elapsed = rest_elapsed or 0 end
    elseif action == "move_to" then
        local target = command.target
        if type(target) == "table" and ani_object and ani_object:get_pos() then
            local tx, ty, tz = tonumber(target.x), tonumber(target.y), tonumber(target.z)
            local current = ani_object:get_pos()
            local goal = tx and ty and tz and {
                x = math.floor(tx + 0.5),
                y = math.floor(ty + 0.5),
                z = math.floor(tz + 0.5),
            } or nil
            if goal and command.reason == "remembered_resource" then
                goal = find_build_resource_approach(goal, current)
            end
            if goal and vector.distance(current, goal) <= 16.0 then
                autonomy_enabled = true
                building_enabled = false
                target_food = nil
                target_tree_apple = nil
                exploration_enabled = true
                exploration_target = goal
                clear_path()
                autonomy_elapsed = 1.0
                success = true
                result = "move_to accepted"
            else
                result = "target too far or invalid"
            end
        else
            result = "body missing or target invalid"
        end
    else
        result = "action not allowed"
    end
    emit_planner_result(action, success, result)
end
local function chat_requests_follow(message)
    local text = message or ""
    local lower = string.lower(text)
    local patterns = {
        "Ая", "ая", "Айа", "айа",
        "иди ко мне", "Иди ко мне",
        "подойди", "Подойди",
        "сюда", "Сюда",
        "за мной", "За мной",
        "следуй", "Следуй",
        "follow", "come here",
    }
    for _, pattern in ipairs(patterns) do
        if string.find(text, pattern, 1, true)
                or string.find(lower, pattern, 1, true) then
            return true
        end
    end
    return false
end

-- Прямой локальный вызов не зависит от Python/Groq и может прервать строительство,
-- подойти к игроку, а затем вернуть прежнюю строительную цель.
function anima_bridge_handle_chat(name, message)
    if not chat_requests_follow(message) then return false end
    local player = minetest.get_player_by_name(name)
    if not player or not ani_object or not ani_object:get_pos() then
        return false
    end
    local target = player:get_pos()
    apply_ai_command({
        action = "socialize",
        player = name,
        target = target,
        duration = SOCIAL_DURATION,
        reason = "direct_chat_call",
    })
    minetest.log("action", "[anima_bridge] direct player call: following " .. tostring(name))
    return true
end

local function poll_ai_command()
    if not command_io or not command_io.open then return end
    local handle = command_io.open(AI_COMMAND_PATH, "r")
    if not handle then return end
    local payload = handle:read("*a")
    handle:close()
    if command_os and command_os.remove then
        command_os.remove(AI_COMMAND_PATH)
    end
    if not payload or #payload > 4096 then return end
    local ok, command = pcall(minetest.parse_json, payload)
    if ok and type(command) == "table" then
        apply_ai_command(command)
    else
        minetest.log("warning", "[anima_bridge] invalid planner command")
    end
end

local function poll_development()
    if not command_io or not command_io.open then return end
    local handle = command_io.open(DEVELOPMENT_PATH, "r")
    if not handle then return end
    local payload = handle:read(65537)
    handle:close()
    if not payload or #payload > 65536 then return end
    local ok, config = pcall(minetest.parse_json, payload)
    if not ok or not development.valid_config(config) then return end
    development_summary = config.summary or {}
    if config.revision == development_revision or building_enabled then return end
    walk_speed = config.body.walk_speed
    JUMP_COOLDOWN = config.body.jump_cooldown
    prefer_resource_memory = config.gathering.prefer_memory
    build_spec = config.construction
    BUILD_HALF_SIZE = build_spec.half_size
    BUILD_WALL_HEIGHT = build_spec.wall_height
    BUILD_ROOF_LEVEL = build_spec.wall_height
    development_revision = config.revision
    emit_world_event("development_applied", {revision = development_revision})
end

minetest.register_chatcommand("anima_evolution", {
    params = "[status|rollback]",
    description = "Развитие Aya: опыт, пробная версия, откат",
    func = function(name, param)
        if param == "rollback" then
            emit_world_event("development_request", {action = "rollback", requester = name})
            return true, "Запрос отката пробной версии отправлен мосту."
        end
        if param ~= "" and param ~= "status" then return false, "Использование: /anima_evolution status|rollback" end
        local trial = development_summary.trial
        local count = 0
        for _ in pairs(development_summary.competence or {}) do count = count + 1 end
        return true, string.format("Развитие: версия %d, изучаемых навыков %d, ресурсов в памяти %d, проба: %s",
            development_revision, count, development_summary.known_resources or 0,
            type(trial) == "table" and (trial.domain .. ", результатов " .. trial.results) or "нет")
    end,
})

minetest.register_globalstep(function(dtime)
    development_poll_elapsed = development_poll_elapsed + dtime
    if development_poll_elapsed >= 2 then
        development_poll_elapsed = 0
        poll_development()
    end
    ai_command_elapsed = ai_command_elapsed + dtime
    if ai_command_elapsed >= AI_COMMAND_POLL_INTERVAL then
        ai_command_elapsed = 0.0
        poll_ai_command()
    end
    if not ani_object or not ani_object:get_pos() then return end
    local current_safety = update_safety_state(dtime)
    if rest_elapsed ~= nil and current_safety.mode then
        rest_elapsed = nil
        autonomy_enabled = true
    end
    if rest_elapsed ~= nil then
        local velocity = ani_object:get_velocity() or vector.zero()
        if math.abs(velocity.x) + math.abs(velocity.y) + math.abs(velocity.z) < 0.1 then
            rest_elapsed = rest_elapsed + dtime
        else
            rest_elapsed = 0
        end
        if rest_elapsed >= 20 then
            emit_world_event("rest_completed", {seconds = rest_elapsed, protected = current_safety.protected})
            rest_elapsed = nil
            autonomy_enabled = true
            exploration_enabled = true
        end
    end
    food_check_elapsed = food_check_elapsed + dtime
    if food_check_elapsed >= 1.0 then
        food_check_elapsed = 0.0
        try_eat_nearby()
    end
    observation_elapsed = observation_elapsed + dtime
    if observation_elapsed >= OBSERVATION_INTERVAL then
        observation_elapsed = 0.0
        observe_world()
    end
    local escaping_liquid = run_liquid_escape(dtime)
    if autonomy_enabled and not escaping_liquid then
        autonomy_elapsed = autonomy_elapsed + dtime
        if target_food and not target_food:get_pos() then
            target_food = nil
            target_food_name = nil
            clear_path()
        end
        if not social_target and not current_safety.mode and not building_enabled
                and not target_food and not target_tree_apple
                and autonomy_elapsed >= 1.0 then
            target_food = find_nearest_food()
            if target_food then
                exploration_target = nil
                target_tree_apple = nil
                clear_path()
                memory.remember("food_found", {item = target_food_name})
                minetest.log("action", "[anima_bridge] food target: " .. tostring(target_food_name))
            else
                target_tree_apple = find_nearest_tree_apple()
                if target_tree_apple then
                    exploration_target = nil
                    clear_path()
                    memory.remember("food_found", {
                        item = "rp_default:apple",
                        source = "tree",
                        position = target_tree_apple.position,
                    })
                    minetest.log("action", string.format(
                        "[anima_bridge] tree apple target=(%d,%d,%d)",
                        target_tree_apple.position.x,
                        target_tree_apple.position.y,
                        target_tree_apple.position.z))
                end
            end
        end
        if current_safety.mode == "shelter" then
            run_safety_mode(current_safety, dtime)
        elseif social_target then
            run_social_mode(dtime)
        elseif current_safety.mode == "player" then
            run_safety_mode(current_safety, dtime)
        elseif building_enabled then
            run_building(dtime)
        elseif target_food then
            local distance = vector.distance(ani_object:get_pos(), target_food:get_pos())
            if distance <= 1.5 then
                stop_autonomous_motion()
                local ate = try_eat_nearby()
                if ate or not target_food:get_pos() then
                    memory.remember("food_reached", {item = target_food_name})
                    target_food = nil
                    clear_path()
                end
            else
                if autonomy_elapsed >= 1.0 then
                    autonomy_elapsed = 0.0
                    plan_food_path()
                end
                follow_food_path(dtime)
            end
        elseif target_tree_apple then
            local apple_pos = target_tree_apple.position
            local node = minetest.get_node_or_nil(apple_pos)
            if not node or not is_tree_apple_node(node.name)
                    or hunger >= HUNGER_MAX and saturation >= SATURATION_MAX then
                memory.remember("apple_lost", {position = apple_pos})
                if node and not is_tree_apple_node(node.name) then
                    emit_world_event("resource_missing", {item = "rp_default:apple", position = apple_pos})
                end
                target_tree_apple = nil
                clear_path()
            else
                local distance = vector.distance(ani_object:get_pos(), apple_pos)
                if distance <= APPLE_REACH then
                    stop_autonomous_motion()
                    harvest_tree_apple()
                else
                    if autonomy_elapsed >= 1.0 then
                        autonomy_elapsed = 0.0
                        if not plan_tree_apple_path() then
                            memory.remember("apple_unreachable", {position = apple_pos})
                            emit_world_event("harvest_failed", {item = "rp_default:apple", position = apple_pos})
                            target_tree_apple = nil
                            clear_path()
                        end
                    end
                    if target_tree_apple then follow_food_path(dtime) end
                end
            end
        elseif exploration_enabled then
            exploration_elapsed = exploration_elapsed + dtime
            if not exploration_target then
                exploration_target = find_exploration_target()
                if exploration_target then
                    exploration_elapsed = 0.0
                    clear_path()
                    memory.remember("explore_goal", {position = exploration_target})
                    minetest.log("action", string.format(
                        "[anima_bridge] exploration target=(%d,%d,%d)",
                        exploration_target.x, exploration_target.y, exploration_target.z))
                end
            end
            if exploration_target then
                local distance = vector.distance(ani_object:get_pos(), exploration_target)
                if distance <= 1.5 then
                    memory.remember("explore_reached", {position = exploration_target})
                    exploration_target = nil
                    exploration_elapsed = 0.0
                    clear_path()
                elseif exploration_elapsed >= 20.0 then
                    memory.remember("explore_abandoned", {position = exploration_target})
                    exploration_target = nil
                    exploration_elapsed = 0.0
                    clear_path()
                else
                    if autonomy_elapsed >= 1.0 then
                        autonomy_elapsed = 0.0
                        if not plan_exploration_path() then
                            exploration_target = nil
                            exploration_elapsed = 0.0
                            clear_path()
                        end
                    end
                    follow_food_path(dtime)
                end
            else
                stop_autonomous_motion()
            end
        else
            stop_autonomous_motion()
        end
    end
    need_elapsed = need_elapsed + dtime
    if need_elapsed < 30.0 then return end
    need_elapsed = 0.0
    local velocity = ani_object:get_velocity() or vector.zero()
    local moving = math.abs(velocity.x) + math.abs(velocity.z) > 0.2
    if saturation > 0 then
        saturation = math.max(0, saturation - (moving and 2 or 1))
    else
        hunger = math.max(0, hunger - 1)
    end
    save_needs()
    emit_world_event("needs_changed", {
        hunger = hunger,
        hunger_max = HUNGER_MAX,
        saturation = saturation,
        saturation_max = SATURATION_MAX,
        moving = moving,
    })
    minetest.log("action", string.format(
        "[anima_bridge] needs hunger=%d saturation=%d moving=%s",
        hunger, saturation, tostring(moving)))
end)
