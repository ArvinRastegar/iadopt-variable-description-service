import {
  Constraint,
  Entity,
  Variable,
  VALID_ASYMMETRIC_SYSTEM_PROPERTY_PAIRS,
  VALID_SYMMETRIC_SYSTEM_PROPERTIES,
  type Concept,
} from '@/model/models.js';

/*
 * Model edits behind the inspector. They follow the previous diagram editor (src/lib/addEditor.js
 * in the old frontend) step by step, so the Turtle they produce is the same.
 */

export type Shape = 'simple' | 'asym' | 'sym';
export type AddableToVariable = 'ContextObject' | 'Matrix' | 'StatisticalModifier';

export interface Fields {
  label: string;
  iri: string;
  comment: string;
}

export interface ShapeChoice {
  shape: Shape;
  /** Index into VALID_ASYMMETRIC_SYSTEM_PROPERTY_PAIRS, used when shape is 'asym'. */
  pairIndex: number;
}

export const ASYMMETRIC_PAIRS: string[][] = VALID_ASYMMETRIC_SYSTEM_PROPERTY_PAIRS;

/** Which language a value is stored under: the one getLabel/getComment would show. */
function preferredLanguage(values: Record<string, string>): string {
  const languages = Object.keys(values);
  if (languages.includes('en')) return 'en';
  if (languages.includes('')) return '';
  return languages[0] ?? '';
}

export function fieldsOf(comp: Concept): Fields {
  return {
    label: comp.getLabel() ?? '',
    iri: comp.getIri() ?? '',
    comment: comp.getComment() ?? '',
  };
}

export function shapeOf(comp: Concept): ShapeChoice {
  if (!(comp instanceof Entity) || !comp.isSystem()) return { shape: 'simple', pairIndex: 0 };
  if (comp.isSymmetricSystem()) return { shape: 'sym', pairIndex: 0 };
  const first = comp.getComponentKeys()[0];
  const pairIndex = Math.max(0, ASYMMETRIC_PAIRS.findIndex((pair) => pair.includes(first)));
  return { shape: 'asym', pairIndex };
}

/** Entities that can become systems: matrix, context object and object of interest, as before. */
export function canChangeShape(comp: Concept): boolean {
  return comp instanceof Entity && ['OoI', 'Matrix', 'ContextObject'].includes(comp.getRole());
}

/** The model constructors take plain strings too, although their JSDoc only names language maps. */
type Init = { label?: string; iri?: string; comment?: string };
const init = (value: Init) => value as unknown as ConstructorParameters<typeof Entity>[0];

function placeholderParts(): [Entity, Entity] {
  return [new Entity(init({ label: 'Component 1' })), new Entity(init({ label: 'Component 2' }))];
}

function applyShape(entity: Entity, choice: ShapeChoice) {
  switch (choice.shape) {
    case 'simple':
      // Clear out any system components and their constraints.
      entity.removeComponents();
      break;
    case 'asym': {
      const props = ASYMMETRIC_PAIRS[choice.pairIndex] ?? ASYMMETRIC_PAIRS[0];
      if (entity.isSystem()) {
        const before = entity.getComponentKeys();
        if (!props.includes(before[0])) {
          // changeComponentKeys needs one property per existing part, and a pair has two.
          if (entity.getComponentCount() !== 2) {
            throw new Error('A source and target needs exactly two parts. Make it a single entity first, then choose source and target.');
          }
          entity.changeComponentKeys(props);
        }
      } else {
        const [a, b] = placeholderParts();
        entity.addComponent(props[0], a);
        entity.addComponent(props[1], b);
      }
      break;
    }
    case 'sym': {
      const part = VALID_SYMMETRIC_SYSTEM_PROPERTIES[0];
      if (entity.isSystem()) {
        if (!entity.isSymmetricSystem()) {
          entity.changeComponentKeys(Array(entity.getComponentCount()).fill(part));
        }
      } else {
        const [a, b] = placeholderParts();
        entity.addComponent(part, a);
        entity.addComponent(part, b);
      }
      break;
    }
  }
}

/**
 * Save label, IRI, description and, for entities, the shape. Returns whether the variable's own
 * label changed, which tells mergeCurrentTurtle to keep the SKOS labels in step.
 */
export function applyEdit(comp: Concept, fields: Fields, choice?: ShapeChoice): { syncVariableLabels: boolean } {
  const syncVariableLabels = comp instanceof Variable && (comp.getLabel() ?? '') !== fields.label;

  comp.setLabel(preferredLanguage(comp._label), fields.label);
  comp.setIri(fields.iri.trim() || null);
  comp.setComment(preferredLanguage(comp._comment), fields.comment);

  if (choice && comp instanceof Entity && canChangeShape(comp)) applyShape(comp, choice);
  return { syncVariableLabels };
}

/** What can still be added to the variable: there is at most one matrix and one statistical modifier. */
export function addableToVariable(variable: Variable): AddableToVariable[] {
  const options: AddableToVariable[] = ['ContextObject'];
  if (!variable.getMatrix()) options.push('Matrix');
  if (!variable.getStatisticalModifier()) options.push('StatisticalModifier');
  return options;
}

export function addToVariable(variable: Variable, type: AddableToVariable, fields: Fields, choice?: ShapeChoice): Entity {
  const entity = new Entity(init({
    label: fields.label || undefined,
    iri: fields.iri.trim() || undefined,
    comment: fields.comment || undefined,
  }));
  if (choice && type !== 'StatisticalModifier' && choice.shape !== 'simple') applyShape(entity, choice);

  if (type === 'ContextObject') variable.addContextObject(entity);
  else if (type === 'Matrix') variable.setMatrix(entity);
  else variable.setStatisticalModifier(entity);
  return entity;
}

/** Every component except a constraint can be narrowed by a new constraint. */
export function canAddConstraint(comp: Concept): boolean {
  return !(comp instanceof Constraint) && !(comp instanceof Variable);
}

export function addConstraint(target: Concept, fields: Fields): Constraint {
  const constraint = new Constraint(init({
    label: fields.label || undefined,
    iri: fields.iri.trim() || undefined,
    comment: fields.comment || undefined,
  }));
  target.getVariable().addConstraint(constraint, target as Entity);
  return constraint;
}

/** The variable, its property and object of interest, and system parts cannot be deleted. */
export function canDelete(comp: Concept): boolean {
  return !['Variable', 'OoI', 'Property', 'SystemComponent'].includes(comp.getRole());
}

export function deleteComponent(comp: Concept): void {
  if (comp instanceof Constraint || comp instanceof Entity) comp.remove();
}
