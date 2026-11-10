// Standalone offline cross-language reference. No production dependencies or IO.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

public static class DatasetCanonicalReference
{
    private static void StringValue(StringBuilder result, string value)
    {
        result.Append('"');
        foreach (char c in value)
        {
            if (c < 32 || c > 126) throw new FormatException("ASCII");
            if (c == '"' || c == '\\') result.Append('\\');
            result.Append(c);
        }
        result.Append('"');
    }
    private static void Write(StringBuilder result, JsonElement value, int depth)
    {
        if (depth > 16) throw new FormatException("JSON_DEPTH");
        switch (value.ValueKind)
        {
            case JsonValueKind.Object:
                var properties = new SortedDictionary<string,JsonElement>(StringComparer.Ordinal);
                foreach (var item in value.EnumerateObject())
                {
                    if (properties.ContainsKey(item.Name)) throw new FormatException("DUPLICATE_KEY");
                    properties.Add(item.Name,item.Value);
                }
                result.Append('{'); bool first = true;
                foreach (var item in properties)
                {
                    if (!first) result.Append(','); first = false;
                    StringValue(result,item.Key); result.Append(':'); Write(result,item.Value,depth+1);
                }
                result.Append('}'); break;
            case JsonValueKind.Array:
                result.Append('['); bool firstElement = true;
                foreach (var item in value.EnumerateArray())
                {
                    if (!firstElement) result.Append(','); firstElement = false; Write(result,item,depth+1);
                }
                result.Append(']'); break;
            case JsonValueKind.String: StringValue(result,value.GetString()); break;
            case JsonValueKind.Number:
                string raw=value.GetRawText();
                if (!Regex.IsMatch(raw,@"\A(?:0|[1-9][0-9]*)\z")) throw new FormatException("INTEGER_TOKEN");
                if (!ulong.TryParse(raw,NumberStyles.None,CultureInfo.InvariantCulture,out ulong n) || n > 9007199254740991UL) throw new FormatException("INTEGER");
                result.Append(n.ToString(CultureInfo.InvariantCulture)); break;
            case JsonValueKind.True: result.Append("true"); break;
            case JsonValueKind.False: result.Append("false"); break;
            case JsonValueKind.Null: result.Append("null"); break;
            default: throw new FormatException("JSON_TYPE");
        }
    }
    public static byte[] Canonical(byte[] raw)
    {
        if (raw.Length>=3 && raw[0]==239 && raw[1]==187 && raw[2]==191) throw new FormatException("BOM");
        using var doc=JsonDocument.Parse(raw,new JsonDocumentOptions { MaxDepth=16 });
        var result=new StringBuilder(); Write(result,doc.RootElement,0);
        return Encoding.UTF8.GetBytes(result.ToString());
    }
    public static byte[] Manifest(byte[] raw)
    {
        using var doc=JsonDocument.Parse(raw);
        return Canonical(Encoding.UTF8.GetBytes(doc.RootElement.GetProperty("manifest").GetRawText()));
    }
    public static string Hash(byte[] raw) => Convert.ToHexString(SHA256.HashData(raw)).ToLowerInvariant();
}
