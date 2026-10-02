local navigation = {}

-- A physical body can stand on a block's corner while its rounded centre is
-- over empty space. Use the engine's actual support contact in that case.
-- Keep the normal pathfinder's drop, jump and clearance limits unchanged.
function navigation.find_path(start, goal, search_distance, options, timeout, moveresult)
    local path, reason = rp_pathfinder.find_path(start, goal, search_distance, options, timeout)
    if path then return path, nil, 2 end
    if (reason ~= "pos1_too_high" and reason ~= "pos1_blocked")
            or not moveresult or not moveresult.touching_ground then
        return nil, reason
    end
    local visited = {}
    for _, collision in ipairs(moveresult.collisions or {}) do
        local node = collision.node_pos
        if collision.type == "node" and collision.axis == "y" and node
                and collision.old_velocity and collision.old_velocity.y <= 0 then
            local support = {x = node.x, y = node.y + 1, z = node.z}
            local key = support.x .. ":" .. support.y .. ":" .. support.z
            -- Only consider nearby contacts, not a stale landing location.
            if not visited[key] and vector.distance(start, support) <= 1.75
                    and vector.distance(start, support) > 0 then
                visited[key] = true
                path = rp_pathfinder.find_path(support, goal, search_distance, options, timeout)
                if path then
                    -- Do not skip the support block: first walk onto its centre,
                    -- then follow the route. Physics, not teleportation, moves us.
                    return path, nil, 1
                end
            end
        end
    end
    return nil, reason
end

return navigation
