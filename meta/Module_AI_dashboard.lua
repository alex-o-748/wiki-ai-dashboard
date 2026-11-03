-- Numbers for the AI dashboard on Meta, read from commons:Data:Wikimedia AI/Latest.tab.
-- That page is refreshed weekly by https://github.com/alex-o-748/wiki-ai-dashboard;
-- this module is a copy of meta/Module_AI_dashboard.lua there.
--
--   {{#invoke:AI dashboard|value|content_translation}}            total over all wikis checked
--   {{#invoke:AI dashboard|value|content_translation|en}}         one wiki
--   {{#invoke:AI dashboard|value|community_ai_filter|wikis_nonzero}}
--   {{#invoke:AI dashboard|value|community_ai_filter|wikis_checked}}
--   {{#invoke:AI dashboard|date}}                                  date of the data, e.g. 2 November 2026
--
-- Options for value:
--   link=URL     wrap the number in an external link (not when it is 0 or missing)
--   zero=TEXT    show TEXT instead of 0, e.g. zero=–
--   missing=TEXT show TEXT when there is no value (default –)
--   raw=1        plain digits, no thousands separators, for use in #expr
-- Options for date:
--   format=...   a #time format (default "j F Y"); raw=1 gives YYYY-MM-DD

local p = {}

local DATA_PAGE = 'Wikimedia AI/Latest.tab'
local DEFAULT_MISSING = '–'

local loaded

local function load()
	if loaded then
		return loaded
	end
	local tab = mw.ext.data.get(DATA_PAGE)
	if not tab then
		error('cannot read commons:Data:' .. DATA_PAGE, 0)
	end
	local col = {}
	for i, field in ipairs(tab.schema.fields) do
		col[field.name] = i
	end
	local values, date = {}, nil
	for _, row in ipairs(tab.data) do
		local metric, key = row[col.metric], row[col.wiki]
		values[metric] = values[metric] or {}
		values[metric][key] = row[col.value] -- nil when the wiki could not be read
		date = date or row[col.date]
	end
	loaded = { values = values, date = date }
	return loaded
end

local function trimmed(args, key)
	local v = args[key]
	if v == nil then
		return nil
	end
	v = mw.text.trim(v)
	if v == '' then
		return nil
	end
	return v
end

local function errorText(message)
	return '<strong class="error">AI dashboard: ' .. message .. '</strong>'
end

-- For other modules: the number, or nil if missing. Errors on an unknown metric.
function p._value(metric, key)
	local data = load()
	local byKey = data.values[metric]
	if not byKey then
		error('no metric "' .. tostring(metric) .. '" in commons:Data:' .. DATA_PAGE, 0)
	end
	return byKey[key or 'total']
end

function p.value(frame)
	local args = frame.args
	local metric = trimmed(args, 1)
	if not metric then
		return errorText('value needs a metric name')
	end
	local key = trimmed(args, 2) or 'total'
	local ok, n = pcall(p._value, metric, key)
	if not ok then
		return errorText(n)
	end
	if n == nil then
		return trimmed(args, 'missing') or DEFAULT_MISSING
	end
	if n == 0 and trimmed(args, 'zero') then
		return trimmed(args, 'zero')
	end
	if trimmed(args, 'raw') then
		return string.format('%d', n)
	end
	local text = mw.getContentLanguage():formatNum(n)
	local link = trimmed(args, 'link')
	if link and n ~= 0 then
		return '[' .. link .. ' ' .. text .. ']'
	end
	return text
end

function p.date(frame)
	local args = frame.args
	local ok, data = pcall(load)
	if not ok then
		return errorText(data)
	end
	if not data.date then
		return trimmed(args, 'missing') or DEFAULT_MISSING
	end
	if trimmed(args, 'raw') then
		return data.date
	end
	return mw.getContentLanguage():formatDate(trimmed(args, 'format') or 'j F Y', data.date)
end

return p
