<?xml version="1.0" encoding="UTF-8"?>
<schema xmlns="http://purl.oclc.org/dsdl/schematron" queryBinding="xslt2">
  <title>Calibration certificate demo rules</title>

  <ns prefix="cal" uri="https://validibot.com/examples/calibration-certificate"/>
  <ns prefix="xs" uri="http://www.w3.org/2001/XMLSchema"/>

  <pattern id="certificate-core">
    <rule context="cal:CalibrationCertificate">
      <assert id="CAL-DATE-001" flag="fatal"
        test="xs:date(@issueDate) ge xs:date(cal:calibration/@performedOn)">
        The certificate issue date must not be before the calibration date.
      </assert>

      <assert id="CAL-ACCRED-001" flag="fatal"
        test="not(cal:calibration/@accredited = 'true') or string-length(normalize-space(cal:issuer/@accreditationId)) gt 0">
        Accredited calibrations must include an issuer accreditation identifier.
      </assert>
    </rule>

    <rule context="cal:calibration">
      <assert id="CAL-DATE-002" flag="fatal"
        test="xs:date(@nextDue) gt xs:date(@performedOn)">
        The next due date must be after the calibration date.
      </assert>

      <assert id="CAL-ENV-001" flag="warning"
        test="xs:decimal(cal:environment/@temperatureC) ge 15 and xs:decimal(cal:environment/@temperatureC) le 25">
        Calibration temperature should be between 15 C and 25 C for this demo profile.
      </assert>

      <assert id="CAL-ENV-002" flag="warning"
        test="xs:decimal(cal:environment/@humidityPct) ge 20 and xs:decimal(cal:environment/@humidityPct) le 80">
        Calibration humidity should be between 20 percent and 80 percent for this demo profile.
      </assert>
    </rule>
  </pattern>

  <pattern id="pressure-gauge-rules">
    <rule context="cal:result">
      <assert id="CAL-UNIT-001" flag="fatal"
        test="@unit = ../../cal:asset/@unit">
        Result unit must match the instrument unit.
      </assert>

      <assert id="CAL-POINT-001" flag="fatal"
        test="count(cal:point) ge 3">
        Pressure gauge calibration must include at least three measurement points.
      </assert>
    </rule>

    <rule context="cal:point">
      <assert id="CAL-RANGE-001" flag="fatal"
        test="xs:decimal(@nominal) ge xs:decimal(../../../cal:asset/@rangeMin) and xs:decimal(@nominal) le xs:decimal(../../../cal:asset/@rangeMax)">
        Nominal pressure must be inside the instrument range.
      </assert>

      <assert id="CAL-POINT-002" flag="fatal"
        test="not(preceding-sibling::cal:point) or xs:decimal(@nominal) gt xs:decimal(preceding-sibling::cal:point[1]/@nominal)">
        Measurement points must be listed in increasing nominal-pressure order.
      </assert>

      <assert id="CAL-MATH-001" flag="fatal"
        test="abs(xs:decimal(@correction) - (xs:decimal(@nominal) - xs:decimal(@indicated))) le 0.000001">
        Correction must equal nominal value minus indicated value.
      </assert>

      <assert id="CAL-MATH-002" flag="fatal"
        test="abs(xs:decimal(@expandedUncertainty) - (xs:decimal(@standardUncertainty) * xs:decimal(../@coverageFactor))) le 0.000001">
        Expanded uncertainty must equal standard uncertainty times coverage factor.
      </assert>

      <assert id="CAL-VERDICT-001" flag="fatal"
        test="@verdict = 'review' or ((@verdict = 'pass') = (abs(xs:decimal(@correction)) le xs:decimal(@tolerance)))">
        Pass/fail verdict must match the correction tolerance rule, unless marked review.
      </assert>
    </rule>
  </pattern>
</schema>
