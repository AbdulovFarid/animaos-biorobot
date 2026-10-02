local MOD_NAME = minetest.get_current_modname()
-- Luanti grants this only from init.lua's main scope. Keep it local and pass
-- it directly to our own body chunk; requesting it in body.lua returns nil.
local insecure_environment = minetest.request_insecure_environment
    and minetest.request_insecure_environment()
assert(loadfile(minetest.get_modpath(MOD_NAME) .. "/body.lua"))(insecure_environment)

local RUNTIME_DIR = minetest.get_modpath(MOD_NAME) .. "/runtime"
-- Created by Python before publishing development.json; do not call core.mkdir
-- in a mod directory while Luanti mod security is enabled.
local GAME_HEARTBEAT_PATH = RUNTIME_DIR .. "/heartbeat"
local GAME_HEARTBEAT_INTERVAL = 5.0
local heartbeat_elapsed = 0.0
local bridge_io = insecure_environment and insecure_environment.io
local bridge_os = insecure_environment and insecure_environment.os

local function autostart_python_bridge()
    if minetest.settings:get_bool("anima_bridge_autostart", true) == false then
        return
    end
    if not bridge_os or not bridge_os.execute then
        minetest.log("warning",
            "[anima_bridge] Автозапуск моста недоступен: нужен insecure environment")
        return
    end

    local project_dir = "/home/fargo/PycharmProjects/Biorobot"
    local environment_file = "/home/fargo/.config/biorobot/groq.env"
    local python_bin = project_dir .. "/.venv/bin/python"
    local bridge_script = project_dir .. "/luanti_bridge.py"
    local bridge_log = "/tmp/aya_bridge.log"
    local command = string.format(
        "cd %q && . %q && nohup %q -u %q >> %q 2>&1 &",
        project_dir, environment_file, python_bin, bridge_script, bridge_log)
    local result = bridge_os.execute(command)
    minetest.log("action", "[anima_bridge] автозапуск Python-моста: "
        .. tostring(result))
end

local function touch_game_heartbeat()
    if not bridge_io or not bridge_io.open then
        return
    end
    local handle = bridge_io.open(GAME_HEARTBEAT_PATH, "w")
    if handle then
        handle:write("alive")
        handle:close()
    end
end

minetest.register_on_shutdown(function()
    if minetest.write_json then
        minetest.log("action", "[anima_event] " .. minetest.write_json({
            kind = "world_shutdown",
            data = {},
        }))
    end
    if bridge_os and bridge_os.remove then
        bridge_os.remove(GAME_HEARTBEAT_PATH)
    end
end)

touch_game_heartbeat()
minetest.after(1.0, autostart_python_bridge)

local SAMPLE_INTERVAL = 5.0
local elapsed = 0.0
local sample_count = 0

local function format_pos(pos)
    if not pos then
        return "?"
    end
    return string.format("(%d, %d, %d)",
        math.floor(pos.x), math.floor(pos.y), math.floor(pos.z))
end

local function snapshot(player)
    local pos = player:get_pos()
    return {
        name = player:get_player_name(),
        position = {
            x = pos.x,
            y = pos.y,
            z = pos.z,
        },
        health = player:get_hp(),
        breath = player:get_breath(),
        timeofday = minetest.get_timeofday(),
    }
end

local function status_line(data)
    return string.format(
        "name=%s position=%s health=%s breath=%s",
        data.name, format_pos(data.position), data.health, data.breath)
end

minetest.register_on_joinplayer(function(player)
    local name = player:get_player_name()
    minetest.chat_send_player(name,
        "[AnimaOS] anima_bridge загружен. Введите /anima_status")
    minetest.log("action", "[anima_bridge] player joined: " .. name)
end)

minetest.register_on_chat_message(function(name, message)
    if anima_bridge_handle_chat then
        anima_bridge_handle_chat(name, message)
    end
    if minetest.write_json then
        local player = minetest.get_player_by_name(name)
        local position = player and player:get_pos() or nil
        minetest.log("action", "[anima_event] " .. minetest.write_json({
            kind = "chat",
            data = {name = name, text = message, position = position},
        }))
    end
end)

minetest.register_chatcommand("anima_status", {
    description = "Показать состояние игрового тела Ani",
    func = function(name)
        local player = minetest.get_player_by_name(name)
        if not player then
            return false, "Игрок не найден."
        end
        local data = snapshot(player)
        return true, "[AnimaOS] " .. status_line(data)
    end,
})

minetest.register_chatcommand("anima_listen", {
    description = "Поговорить с Aya через микрофон",
    params = "[секунды]",
    func = function(name, param)
        local seconds = tonumber(param) or 8
        seconds = math.max(2, math.min(15, math.floor(seconds)))
        minetest.chat_send_player(name,
            "[AnimaOS] Говорите в микрофон (" .. seconds .. " сек.).")
        if minetest.write_json then
            minetest.log("action", "[anima_event] " .. minetest.write_json({
                kind = "voice_request",
                data = {name = name, seconds = seconds},
            }))
        end
        return true, "[AnimaOS] Слушаю вас."
    end,
})

minetest.register_globalstep(function(dtime)
    heartbeat_elapsed = heartbeat_elapsed + dtime
    if heartbeat_elapsed >= GAME_HEARTBEAT_INTERVAL then
        heartbeat_elapsed = 0.0
        touch_game_heartbeat()
    end
    elapsed = elapsed + dtime
    if elapsed < SAMPLE_INTERVAL then
        return
    end
    elapsed = 0.0
    sample_count = sample_count + 1
    for _, player in ipairs(minetest.get_connected_players()) do
        local data = snapshot(player)
        minetest.log("action", string.format(
            "[anima_bridge] sample=%d %s", sample_count, status_line(data)))
    end
end)

minetest.log("action", "[anima_bridge] loaded as " .. MOD_NAME)
