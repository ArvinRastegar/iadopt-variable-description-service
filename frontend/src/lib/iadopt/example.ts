/** The example the previous frontend opened with: it shows every kind of component and a system. */
export const EXAMPLE_TURTLE = `@prefix ex: <http://example.org/iadopt/challenge/> .
@prefix iop: <https://w3id.org/iadopt/ont/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

ex:someVar a iop:Variable ;
    rdfs:label "some example variable" ;
    rdfs:comment "used to illustrate the capabilities of the I-Adopt Visualizer" ;
    iop:hasConstraint [
      a iop:Constraint ;
      rdfs:label "some constraint" ;
      iop:constrains ex:Matrix_SysA
    ], [
      a iop:Constraint ;
      rdfs:label "other constraint" ;
      iop:constrains ex:Matrix_SysB
    ], [
      a iop:Constraint ;
      rdfs:label "one more constraint" ;
      iop:constrains ex:StatMod
    ] ;
    iop:hasMatrix ex:Matrix ;
    iop:hasObjectOfInterest ex:OoI ;
    iop:hasStatisticalModifier ex:StatMod ;
    iop:hasProperty ex:Prop .

ex:Matrix
    a iop:Entity, iop:System ;
    rdfs:label "vegetation-soil system" ;
    iop:hasSource ex:Matrix_SysA;
    iop:hasTarget ex:Matrix_SysB.

ex:OoI
    a iop:Entity ;
    rdfs:label "Object of Interest" .

ex:StatMod
    a iop:StatisticalModifier ;
    rdfs:label "Statistical Modifier" .

ex:Prop
    a iop:Property ;
    rdfs:label "Property" .

ex:Matrix_SysA
    a iop:Entity ;
    rdfs:label "System Component A" .

ex:Matrix_SysB
    a iop:Entity ;
    rdfs:label "System Component B" .
`;

/** Example definitions for the composer (from the README and the contract goldens). */
export const EXAMPLE_DEFINITIONS = [
  'Maximum daily air temperature at 2 meters',
  'Moisture in upper soil portion measured in kg m-2',
  'Dynamic shear viscosity of polystyrene PS042 under the testing conditions of DIN 51810-1',
];
