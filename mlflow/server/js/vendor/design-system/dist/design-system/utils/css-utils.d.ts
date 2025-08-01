import type { CSSObject } from '@emotion/react';
import type { CSSProperties } from 'react';
import type { VirtualItem } from 'react-virtual';
import { type Theme } from '../../theme';
import type { TypographyColor, ValidationState } from '../types';
/**
 * Recursively appends `!important` to all CSS properties in an Emotion `CSSObject`.
 * Used to ensure that we always override Ant styles, without worrying about selector precedence.
 */
export declare function importantify(obj: CSSObject): CSSObject;
/**
 * Returns a text color, in case of invalid/missing key and missing fallback color it will return textPrimary
 * @param theme
 * @param key - key of TypographyColor
 * @param fallbackColor - color to return as fallback -- used to remove tertiary check inline
 */
export declare function getTypographyColor(theme: Theme, key?: TypographyColor, fallbackColor?: string): string;
/**
 * Returns validation color based on state, has default validation colors if params are not provided
 * @param theme
 * @param validationState
 * @param errorColor
 * @param warningColor
 * @param successColor
 */
export declare function getValidationStateColor(theme: Theme, validationState?: ValidationState, { errorColor, warningColor, successColor, }?: {
    errorColor?: string;
    warningColor?: string;
    successColor?: string;
}): string | undefined;
export declare function getDarkModePortalStyles(theme: Theme, useNewShadows: boolean, useNewBorderColors: boolean): React.CSSProperties;
type GetShadowScrollFunction = (theme: Theme, options?: {
    backgroundColor?: string;
    orientation?: 'vertical' | 'horizontal';
}) => Pick<CSSObject, 'background' | 'backgroundRepeat' | 'backgroundSize' | 'backgroundAttachment'>;
export declare const getShadowScrollStyles: GetShadowScrollFunction;
export declare const getBottomOnlyShadowScrollStyles: GetShadowScrollFunction;
export declare const getHorizontalTabShadowStyles: (theme: Theme, { backgroundColor }?: {
    backgroundColor?: string;
}) => Pick<CSSObject, "background" | "backgroundRepeat" | "backgroundSize" | "backgroundAttachment">;
export declare function getVirtualizedComboboxMenuItemStyles(virtualItem: VirtualItem): CSSProperties;
export {};
//# sourceMappingURL=css-utils.d.ts.map