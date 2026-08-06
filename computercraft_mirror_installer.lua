-- Yukibot ComputerCraft mirror installer and updater.
-- Usage: wget run <installer-url> <project-id> <project-url> [install-directory] [--enable-startup] [--quiet]

local arguments = { ... }
local project_id = arguments[1]
local mirror_url = arguments[2]
local install_root = nil
local quiet = false
local enable_startup = false

for index = 3, #arguments do
    local argument = arguments[index]
    if argument == "--quiet" then
        quiet = true
    elseif argument == "--enable-startup" then
        enable_startup = true
    elseif not install_root then
        install_root = argument
    else
        print("Usage: wget run <installer-url> <project-id> <project-url> [install-directory] [--enable-startup] [--quiet]")
        return false
    end
end
install_root = install_root or "/"

local function report(message)
    if not quiet then
        print(message)
    end
end

local function fail(message)
    report("Mirror update failed: " .. message)
    return false
end

if type(project_id) ~= "string" or not project_id:match("^[a-z][a-z0-9%-]*$") then
    print("Usage: wget run <installer-url> <project-id> <project-url> [install-directory] [--enable-startup] [--quiet]")
    return false
end
if type(mirror_url) ~= "string" or not mirror_url:match("^https?://") then
    return fail("project URL must use HTTP or HTTPS")
end
if not http then
    return fail("HTTP is disabled on this computer")
end

mirror_url = mirror_url:gsub("/+$", "")
install_root = install_root:gsub("/+$", "")
if install_root == "" then
    install_root = "/"
elseif install_root:sub(1, 1) ~= "/" then
    install_root = "/" .. install_root
end
if enable_startup and install_root == "/" then
    return fail("automatic boot mode requires an install directory other than /")
end

local api_root = mirror_url:match("^(https?://.-/mirror/v1)/projects/[^/]+$")
if not api_root then
    return fail("project URL must end with /mirror/v1/projects/<project-id>")
end

local function valid_file_path(path)
    if type(path) ~= "string" or path == "" or path:find("\\", 1, true) or path:find("//", 1, true) then
        return false
    end
    for part in path:gmatch("[^/]+") do
        if part == "." or part == ".." then
            return false
        end
    end
    return true
end

local function encode_path(path)
    return (path:gsub("[^%w%-%._~/]", function(character)
        return string.format("%%%02X", string.byte(character))
    end))
end

local function read_json(path)
    if not fs.exists(path) or fs.isDir(path) then
        return nil
    end
    local handle = fs.open(path, "r")
    if not handle then
        return nil
    end
    local raw = handle.readAll()
    handle.close()
    local ok, value = pcall(textutils.unserializeJSON, raw)
    return ok and type(value) == "table" and value or nil
end

local function remove_temporary_file(path)
    if not fs.exists(path) then
        return true
    end
    if fs.isDir(path) then
        return false, "temporary path is a directory: " .. path
    end
    fs.delete(path)
    return true
end

local function replace_temporary_file(temporary_path, target_path)
    local backup_path = target_path .. ".yukibot-mirror.previous"
    local backup_removed, backup_problem = remove_temporary_file(backup_path)
    if not backup_removed then
        return false, backup_problem
    end
    if fs.exists(target_path) then
        if fs.isDir(target_path) then
            return false, "target is a directory: " .. target_path
        end
        fs.move(target_path, backup_path)
    end
    local ok, problem = pcall(fs.move, temporary_path, target_path)
    if not ok then
        if fs.exists(backup_path) and not fs.exists(target_path) then
            fs.move(backup_path, target_path)
        end
        return false, tostring(problem)
    end
    if fs.exists(backup_path) then
        local removed, problem = remove_temporary_file(backup_path)
        if not removed then
            return false, problem
        end
    end
    return true
end

local function write_text_atomic(path, contents)
    local directory = fs.getDir(path)
    if directory ~= "" and not fs.exists(directory) then
        fs.makeDir(directory)
    end
    local temporary_path = path .. ".yukibot-mirror.next"
    local removed, problem = remove_temporary_file(temporary_path)
    if not removed then
        return false, problem
    end
    local handle = fs.open(temporary_path, "w")
    if not handle then
        return false, "could not open " .. temporary_path
    end
    handle.write(contents)
    handle.close()
    return replace_temporary_file(temporary_path, path)
end

local function response_code(response)
    return response and response.getResponseCode and response.getResponseCode() or 0
end

local function fetch_manifest()
    local response, message, failed_response = http.get(mirror_url .. "/manifest.json", { ["Cache-Control"] = "no-cache" })
    if not response then
        local code = response_code(failed_response)
        if failed_response then
            failed_response.close()
        end
        return nil, code == 0 and (message or "could not fetch manifest") or ("HTTP " .. code)
    end
    local raw = response.readAll()
    response.close()
    local ok, manifest = pcall(textutils.unserializeJSON, raw)
    if not ok or type(manifest) ~= "table" then
        return nil, "manifest is not valid JSON"
    end
    if manifest.schema ~= 1 or manifest.project ~= project_id or type(manifest.revision) ~= "string" or type(manifest.files) ~= "table" then
        return nil, "manifest has an unsupported format"
    end
    return manifest
end

local function download_file(entry, revision)
    local target_path = fs.combine(install_root, entry.path)
    local directory = fs.getDir(target_path)
    if directory ~= "" and not fs.exists(directory) then
        fs.makeDir(directory)
    end
    local temporary_path = target_path .. ".yukibot-mirror.next"
    local removed, problem = remove_temporary_file(temporary_path)
    if not removed then
        return false, problem
    end
    local response, message, failed_response =
        http.get(mirror_url .. "/files/" .. encode_path(entry.path) .. "?revision=" .. revision, { ["Cache-Control"] = "no-cache" })
    if not response then
        local code = response_code(failed_response)
        if failed_response then
            failed_response.close()
        end
        if code == 409 then
            return false, "snapshot_changed"
        end
        return false, code == 0 and (message or "could not download " .. entry.path) or ("HTTP " .. code)
    end
    local handle = fs.open(temporary_path, "w")
    if not handle then
        response.close()
        return false, "could not write " .. temporary_path
    end
    local size = 0
    while true do
        local chunk = response.read(8192)
        if not chunk then
            break
        end
        size = size + #chunk
        handle.write(chunk)
    end
    handle.close()
    response.close()
    if size ~= entry.size then
        remove_temporary_file(temporary_path)
        return false, "download size did not match manifest for " .. entry.path
    end
    return replace_temporary_file(temporary_path, target_path)
end

local function updater_script(boot_updates_enabled)
    local quote = string.format
    local arguments = quote("%q", "wget")
        .. ", "
        .. quote("%q", "run")
        .. ", "
        .. quote("%q", api_root .. "/installer.lua")
        .. ", "
        .. quote("%q", project_id)
        .. ", "
        .. quote("%q", mirror_url)
        .. ", "
        .. quote("%q", install_root)
        .. ", "
        .. quote("%q", "--quiet")
    if boot_updates_enabled then
        arguments = arguments .. ", " .. quote("%q", "--enable-startup")
    end
    return "return shell.run(" .. arguments .. ")\n"
end

local function startup_dispatcher_script(state_root)
    local quote = string.format
    return "-- Yukibot mirror boot updater.\n"
        .. "local state_root = "
        .. quote("%q", state_root)
        .. "\n"
        .. "if fs.exists(state_root) and fs.isDir(state_root) then\n"
        .. "  for _, name in ipairs(fs.list(state_root)) do\n"
        .. "    if name:match("
        .. quote("%q", "^[a-z][a-z0-9%-]*%.lua$")
        .. ") then\n"
        .. "      pcall(function() shell.run(fs.combine(state_root, name)) end)\n"
        .. "    end\n"
        .. "  end\n"
        .. "end\n"
end

local function system_startup_script(dispatcher_path, program_startup_path)
    local quote = string.format
    local contents = "-- Managed Yukibot mirror boot updater.\n" .. "pcall(function() shell.run(" .. quote("%q", dispatcher_path) .. ") end)\n"
    if not program_startup_path then
        return contents
    end
    return contents .. "shell.run(" .. quote("%q", program_startup_path) .. ")\n"
end

local function enable_boot_updates(state_root, program_startup_path)
    local startup_path = "/startup.lua"
    local dispatcher_path = fs.combine(state_root, "_startup.lua")
    local contents = system_startup_script(dispatcher_path, program_startup_path)
    local legacy_contents = system_startup_script(dispatcher_path, nil)
    if fs.exists(startup_path) then
        if fs.isDir(startup_path) then
            report("Automatic boot updates were not enabled because /startup.lua is a directory.")
            return true
        end
        local handle = fs.open(startup_path, "r")
        local existing = handle and handle.readAll() or nil
        if handle then
            handle.close()
        end
        if existing == contents then
            return true
        end
        if existing == legacy_contents then
            local saved, problem = write_text_atomic(startup_path, contents)
            if not saved then
                return false, problem
            end
            report("Updated the Yukibot-managed startup.lua to launch this mirror after updating.")
            return true
        end
        report("Automatic boot updates were not enabled because /startup.lua already exists. Add the Yukibot updater snippet to it instead.")
        return true
    end
    local saved, problem = write_text_atomic(startup_path, contents)
    if not saved then
        return false, problem
    end
    report("Automatic Yukibot mirror updates will now run at boot.")
    return true
end

local function sync_once()
    local manifest, manifest_problem = fetch_manifest()
    if not manifest then
        return false, manifest_problem
    end
    local state_root = "__YUKIBOT_MIRROR_STATE_ROOT__"
    local state_path = fs.combine(state_root, project_id .. ".json")
    local previous_state = read_json(state_path)
    if previous_state and previous_state.destination ~= install_root then
        return false, "this mirror is already installed at " .. tostring(previous_state.destination)
    end
    local boot_updates_enabled = enable_startup or (previous_state and previous_state.boot_updates_enabled == true) or false
    local previous_files = {}
    if previous_state and type(previous_state.files) == "table" then
        for _, entry in ipairs(previous_state.files) do
            if type(entry) == "table" and valid_file_path(entry.path) then
                previous_files[entry.path] = entry
            end
        end
    end
    local next_files = {}
    local state_files = {}
    for _, entry in ipairs(manifest.files) do
        if
            type(entry) ~= "table"
            or not valid_file_path(entry.path)
            or type(entry.size) ~= "number"
            or entry.size < 0
            or entry.size % 1 ~= 0
            or type(entry.sha256) ~= "string"
        then
            return false, "manifest contains an invalid file entry"
        end
        if next_files[entry.path] then
            return false, "manifest contains a duplicate file path"
        end
        next_files[entry.path] = entry
        table.insert(state_files, { path = entry.path, size = entry.size, sha256 = entry.sha256 })
    end
    if boot_updates_enabled and not next_files["startup.lua"] then
        return false, "automatic boot mode requires startup.lua at the mirror root"
    end
    for _, entry in ipairs(manifest.files) do
        local previous = previous_files[entry.path]
        local target_path = fs.combine(install_root, entry.path)
        if not previous and fs.exists(target_path) then
            return false, "refusing to replace unmanaged file: " .. target_path
        end
        local already_current = previous
            and previous.sha256 == entry.sha256
            and previous.size == entry.size
            and fs.exists(target_path)
            and not fs.isDir(target_path)
            and fs.getSize(target_path) == entry.size
        if not already_current then
            local downloaded, download_problem = download_file(entry, manifest.revision)
            if not downloaded then
                return false, download_problem
            end
        end
    end
    for path in pairs(previous_files) do
        if not next_files[path] then
            local target_path = fs.combine(install_root, path)
            if fs.exists(target_path) and not fs.isDir(target_path) then
                fs.delete(target_path)
            end
        end
    end
    if not fs.exists(state_root) then
        fs.makeDir(state_root)
    end
    local state = {
        schema = 1,
        project = project_id,
        destination = install_root,
        revision = manifest.revision,
        boot_updates_enabled = boot_updates_enabled,
        files = state_files,
    }
    local saved, save_problem = write_text_atomic(state_path, textutils.serializeJSON(state))
    if not saved then
        return false, save_problem
    end
    local updater_path = fs.combine(state_root, project_id .. ".lua")
    local updater_saved, updater_problem = write_text_atomic(updater_path, updater_script(boot_updates_enabled))
    if not updater_saved then
        return false, updater_problem
    end
    local dispatcher_path = fs.combine(state_root, "_startup.lua")
    local dispatcher_saved, dispatcher_problem = write_text_atomic(dispatcher_path, startup_dispatcher_script(state_root))
    if not dispatcher_saved then
        return false, dispatcher_problem
    end
    if boot_updates_enabled then
        local startup_enabled, startup_problem = enable_boot_updates(state_root, fs.combine(install_root, "startup.lua"))
        if not startup_enabled then
            return false, startup_problem
        end
    end
    report("Mirror " .. project_id .. " is at revision " .. manifest.revision:sub(1, 12))
    return true
end

for attempt = 1, 2 do
    local updated, problem = sync_once()
    if updated then
        return true
    end
    if problem ~= "snapshot_changed" then
        return fail(problem)
    end
    report("Mirror changed while updating; retrying once.")
end
return fail("mirror changed repeatedly; try again shortly")
