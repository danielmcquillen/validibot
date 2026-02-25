# Data Paths

When you define a signal for a validator, the **data path** tells Validibot where to find that value in your data. Think of it as an address pointing to a specific piece of information inside a JSON object or XML document.

If your signal's slug already matches a top-level key in the data, the data path can be the same as the slug. For nested values, you use dot notation (JSON) or slash notation (XML) to drill down.

---

## JSON Data Paths

### Top-level fields

For flat JSON, the data path is simply the key name.

Given this JSON:

```json
{
  "sku": "ABCD1234",
  "name": "Widget Mini",
  "price": 20.00,
  "rating": 95,
  "in_stock": true
}
```

| Signal slug | Data path | Value |
|-------------|-----------|-------|
| `price` | `price` | `20.00` |
| `rating` | `rating` | `95` |
| `in-stock` | `in_stock` | `true` |

Notice that the slug uses hyphens (it's a slug) while the data path matches the actual key name with underscores.

### Nested objects

Use **dot notation** to access values inside nested objects.

```json
{
  "name": "Widget Mini",
  "dimensions": {
    "width": 3.5,
    "height": 1.2
  }
}
```

| Signal slug | Data path | Value |
|-------------|-----------|-------|
| `width` | `dimensions.width` | `3.5` |
| `height` | `dimensions.height` | `1.2` |

You can go as deep as you need: `building.envelope.wall.u_value` works for deeply nested structures.

### Arrays

Use **bracket notation** with a zero-based index to access array elements.

```json
{
  "tags": ["gadgets", "mini"],
  "results": [
    { "zone": "North", "temp": 21.3 },
    { "zone": "South", "temp": 23.1 }
  ]
}
```

| Signal slug | Data path | Value |
|-------------|-----------|-------|
| `first-tag` | `tags[0]` | `"gadgets"` |
| `north-temp` | `results[0].temp` | `21.3` |
| `south-zone` | `results[1].zone` | `"South"` |

---

## XML Data Paths

For XML data, use **slash notation** to navigate the element tree, similar to XPath.

### Simple elements

```xml
<product>
  <sku>ABCD1234</sku>
  <name>Widget Mini</name>
  <price>20.00</price>
</product>
```

| Signal slug | Data path | Value |
|-------------|-----------|-------|
| `price` | `product/price` | `20.00` |
| `name` | `product/name` | `"Widget Mini"` |

### Nested elements

```xml
<building>
  <envelope>
    <wall>
      <u_value>0.35</u_value>
      <area>120.5</area>
    </wall>
  </envelope>
</building>
```

| Signal slug | Data path | Value |
|-------------|-----------|-------|
| `wall-u-value` | `building/envelope/wall/u_value` | `0.35` |
| `wall-area` | `building/envelope/wall/area` | `120.5` |

### Attributes

Use `@` to access XML attributes.

```xml
<zone id="north" type="conditioned">
  <temperature unit="C">21.3</temperature>
</zone>
```

| Signal slug | Data path | Value |
|-------------|-----------|-------|
| `zone-id` | `zone/@id` | `"north"` |
| `temp-unit` | `zone/temperature/@unit` | `"C"` |

---

## Tips

- **Case-sensitive**: Data paths are case-sensitive. `Price` and `price` are different paths.
- **Slug vs data path**: If your signal slug matches a top-level key in the data, you can use the slug as the data path. For nested data, the data path will differ from the slug.
- **Whitespace**: Don't include spaces in data paths. `dimensions.width` is correct, `dimensions . width` is not.
- **Special characters**: If a JSON key contains dots or brackets, wrap it in quotes: `["my.field"]`.

For more on writing rules against your signals, see the [CEL Expressions](/app/help/concepts/cel-expressions/) guide.
