import {Composition} from 'remotion';
import {Omaform, TOTAL} from './Omaform';

export const Root: React.FC = () => (
  <Composition id="Omaform" component={Omaform} durationInFrames={TOTAL}
    fps={30} width={1920} height={1080} />
);
