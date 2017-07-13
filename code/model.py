######################################################################
#  CliNER - model.py                                                 #
#                                                                    #
#  Willie Boag                                                       #
#                                                                    #
#  Purpose: Define the model for clinical concept extraction.        #
######################################################################

from __future__ import with_statement

from sklearn.feature_extraction  import DictVectorizer
import os

import feature_extraction.features as feat_obj

from feature_extraction.utilities import load_pickled_obj, is_prose_sentence
from feature_extraction.read_config import enabled_modules

from machine_learning import sci
from machine_learning import crf

from notes.note import concept_labels, reverse_concept_labels
from notes.note import     IOB_labels,     reverse_IOB_labels
from tools      import flatten, save_list_structure, reconstruct_list

from collections import defaultdict

# Stores the verbosity
import globals_cliner
import numpy as np


CLINER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))



class ClinerModel:

    @staticmethod
    def load(filename='awesome.model'):
        model = load_pickled_obj(filename)
        model.filename = filename

        return model


    def __init__(self, use_lstm):

        """
        ClinerModel::__init__()

        Instantiate a ClinerModel object.

        @param use_lstm. Bool indicating whether to train a CRF or LSTM.
        """
        self._use_lstm       = use_lstm
        self._is_trained     = False
        self._clf            = None
        self._vocab          = None
        self._training_files = None
        self._log            = None



    def train(self, notes):
        """
        ClinerModel::train()

        Purpose: Train a Machine Learning model on annotated data

        @param notes. A list of Note objects (containing text and annotations)
        @return       None
        """

        # Extract formatted data
        tokenized_sentences = flatten([n.getTokenizedSentences() for n in notes])
        labels = flatten([n.getTokenLabels() for n in notes])
        
        self.train_fit(tokenized_sents, labels, dev_split=0.1)

    def train_fit(self,tok_sents, tags, val_sents=None, val_tgs=None, dev_split=None):
        """
        ClinerModel::train_ft()

        Purpose: Train clinical concept extraction model using annotated data.

        @param tok_sents.   A list of sentences, where each sentence is tokenized into words.
        @param tags.        Parallel to 'tokenized_sents', 7-way labels for concept spans.
        @param val_sents.   Validation data. Same format as tokenized_sents
        @param val_tags.    Validation data. Same format as iob_nested_labels
        @param dev_split    A real number from 0 to 1
        """

        # train classifier
        voc, clf, dev_score = generic_train('all', tok_sents, tags, self._use_lstm,
                                            val_sents=val_sents, val_labels=val_tags,
                                            dev_split=dev_split)
        self._is_trained = True
        self._vocab = voc
        self._clf   = clf
        self._score = dev_score


    def predict(self, note):
        """
        ClinerModel::predict()

        Purpose: Predict concept annotations for a given note

        @param note. A Note object (containing text and annotations)
        @return      <list> of Classification objects
        """
        # First pass (IOB Chunking)
        if globals_cliner.verbosity > 0:
            print 'first pass'

        # Extract formatted data
        tokenized_sentences =  first_pass_data(note)

        # Predict IOB labels
        iobs = self.__first_predict(tokenized_sentences)
        note.setIOBLabels(iobs)

        # Second pass (concept labels)
        if globals_cliner.verbosity > 0:
            print 'second pass'

        # Extract formatted data
        chunked_sentences, inds = second_pass_data(note)

        # Predict concept labels
        classifications = self.__second_predict(chunked_sentences, inds)

        result = classifications
        return result



    #########################################################################
    ##           Mid-level reformats data and sends to lower level         ##
    #########################################################################

    def __first_train(self, tokenized_sentences, Y):

        """
        ClinerModel::__first_train()

        Purpose: Train the first pass classifiers (for IOB chunking)

        @param tokenized_sentences. <list> of tokenized sentences
        @param Y.                   <list-of-lists> of IOB labels for words

        @return          None
        """

        if globals_cliner.verbosity > 0: print 'first pass'
        if globals_cliner.verbosity > 0: print '\textracting  features (pass one)'

        # Seperate into prose v nonprose
        nested_prose_data   ,    nested_prose_Y = zip(*filter(lambda line_iob_tup:     is_prose_sentence(line_iob_tup[0]), zip(tokenized_sentences,Y) ))
        nested_nonprose_data, nested_nonprose_Y = zip(*filter(lambda line_iob_tup: not is_prose_sentence(line_iob_tup[0]), zip(tokenized_sentences,Y) ))


        #extract features
        nested_prose_feats    = feat_obj.IOB_prose_features(      nested_prose_data)
        nested_nonprose_feats = feat_obj.IOB_nonprose_features(nested_nonprose_data)

        # Flatten lists (because classifier will expect flat)
        prose_Y        = flatten(nested_prose_Y       )
        nonprose_Y     = flatten(nested_nonprose_Y    )

        # rename because code uses it
        pchunks  =    prose_Y
        nchunks  = nonprose_Y
        prose    =    nested_prose_feats
        nonprose = nested_nonprose_feats

        # Train classifiers for prose and nonprose
        pvec, pclf = self.__generic_first_train(   'prose',    prose, pchunks)
        nvec, nclf = self.__generic_first_train('nonprose', nonprose, nchunks)

        # Save vectorizers
        self._first_prose_vec    = pvec
        self._first_nonprose_vec = nvec

        # Save classifiers
        self._first_prose_clf    = pclf
        self._first_nonprose_clf = nclf



    def __first_predict(self, data):

        """
        ClinerModel::__first_predict()

        Purpose: Predict IOB chunks on data

        @param data.  A list of split sentences    (1 sent = 1 line from file)
        @return       A list of list of IOB labels (1:1 mapping with data)
        """

        if globals_cliner.verbosity > 0:print '\textracting  features (pass one)'

        # Seperate into
        nested_prose_data    = filter(lambda line:     is_prose_sentence(line), data)
        nested_nonprose_data = filter(lambda line: not is_prose_sentence(line), data)

        # Parition into prose v. nonprose
        nested_prose_feats    = feat_obj.IOB_prose_features(      nested_prose_data)
        nested_nonprose_feats = feat_obj.IOB_nonprose_features(nested_nonprose_data)

        # rename because code uses it
        prose    =    nested_prose_feats
        nonprose = nested_nonprose_feats

        # Predict labels for IOB prose and nonprose text
        nlist = self.__generic_first_predict('nonprose', nonprose, self._first_nonprose_vec, self._first_nonprose_clf)
        plist = self.__generic_first_predict(   'prose',    prose, self._first_prose_vec   , self._first_prose_clf   )


        # Stitch prose and nonprose data back together
        # translate IOB labels into a readable format
        prose_iobs    = []
        nonprose_iobs = []
        iobs          = []
        num2iob = lambda l: reverse_IOB_labels[int(l)]
        for sentence in data:
            if sentence == []:
                iobs.append( [] )
            elif is_prose_sentence(sentence):
                prose_iobs.append( plist.pop(0) )
                prose_iobs[-1] = map(num2iob, prose_iobs[-1])
                iobs.append( prose_iobs[-1] )
            else:
                nonprose_iobs.append( nlist.pop(0) )
                nonprose_iobs[-1] = map(num2iob, nonprose_iobs[-1])
                iobs.append( nonprose_iobs[-1] )

        # list of list of IOB labels
        return iobs




    def __second_predict(self, chunked_sentences, inds_list):

        # If first pass predicted no concepts, then skip
        # NOTE: Special case because SVM cannot have empty input
        if sum([ len(inds) for inds in inds_list ]) == 0:
            print "first pass predicted no concepts, skipping second pass"
            return []

        # Create object that is a wrapper for the features
        if globals_cliner.verbosity > 0: print '\textracting  features (pass two)'

        print '\textracting  features (pass two)'

        # Extract features
        text_features = [ feat_obj.concept_features(s,inds) for s,inds in zip(chunked_sentences,inds_list) ]
        flattened_text_features = flatten(text_features)


        print '\tvectorizing features (pass two)'

        if globals_cliner.verbosity > 0: print '\tvectorizing features (pass two)'

        # Vectorize features
        vectorized_features = self._second_vec.transform(flattened_text_features)

        if globals_cliner.verbosity > 0: print '\tpredicting    labels (pass two)'

        # Predict concept labels
        out = sci.predict(self._second_clf, vectorized_features)

        # Line-by-line processing
        o = list(out)
        classifications = []
        for lineno,inds in enumerate(inds_list):

            # Skip empty line
            if not inds: continue

            # For each concept
            for ind in inds:

                # Get next concept
                concept = reverse_concept_labels[o.pop(0)]

                # Get start position (ex. 7th word of line)
                start = 0
                for i in range(ind):
                    start += len( chunked_sentences[lineno][i].split() )

                # Length of chunk
                length = len(chunked_sentences[lineno][ind].split())

                # Classification token
                classifications.append( (concept,lineno+1,start,start+length-1) )

        # Return classifications
        return classifications


    def __third_predict(self, chunks, classifications, inds):

        print '\textracting  features (pass three)'

        # Extract features between pairs of chunks
        unvectorized_X = feat_obj.extract_third_pass_features(chunks, inds, bow=self.bow)

        print '\tvectorizing features (pass three)'

        # Vectorize features
        X = self.third_vec.transform(unvectorized_X)

        print '\tpredicting    labels (pass three)'

        # Predict concept labels
        predicted_relationships = sci.predict(self.third_clf, X)

        classifications_cpy = list(classifications)

        # Stitch SVM output into clustered token span classifications
        clustered = []
        for indices in inds:

            # Cannot have pairwise relationsips with either 0 or 1 objects
            if len(indices) == 0:
                continue

            elif len(indices) == 1:
                # Contiguous span (adjust format to (length-1 list of tok spans)
                tup = list(classifications_cpy.pop(0))
                tup = (tup[0],tup[1],[(tup[2],tup[3])])
                clustered.append(tup)

            else:

                # Number of classifications on the line
                tups = []
                for _ in range(len(indices)):
                    tup = list(classifications_cpy.pop(0))
                    tup = (tup[0],tup[1],[(tup[2],tup[3])])
                    tups.append(tup)

                # Pairwise clusters
                clusters = {}

                # ASSUMPTION: All classifications have same label
                concept = tups[0][0]
                lineno = tups[0][1]
                spans = map(lambda t:t[2][0], tups)

                # Keep track of non-clustered spans
                singulars = list(tups)

                # Get all pairwise relationships for the line
                for i in range(len(indices)):
                    for j in range(i+1,len(indices)):
                        pair = predicted_relationships.pop(0)
                        if pair == 1:
                            tup = (concept,lineno,[spans[i],spans[j]])
                            clustered.append(tup)

                            # No longer part of a singular span
                            if tups[i] in singulars:
                                singulars.remove(tups[i])
                            if tups[j] in singulars:
                                singulars.remove(tups[j])

                clustered += singulars

        return clustered



    ############################################################################
    ###               Lowest-level (interfaces to ML modules)                ###
    ############################################################################


    def __generic_first_train(self, p_or_n, text_features, iob_labels):

        '''
        ClinerModel::__generic_first_train()

        Purpose: Train that works for both prose and nonprose

        @param p_or_n.        <string> either "prose" or "nonprose"
        @param text_features. <list-of-lists> of feature dictionaries
        @param iob_labels.    <list> of "I", "O", and "B" labels
        '''

        # Must have data to train on
        if len(text_features) == 0:
            raise Exception('Training must have %s training examples' % p_or_n)

        # Vectorize IOB labels
        Y_labels = [  IOB_labels[y]  for  y  in  iob_labels  ]

        # Save list structure to reconstruct after vectorization
        offsets = save_list_structure(text_features)

        if globals_cliner.verbosity > 0: print '\tvectorizing features (pass one) ' + p_or_n

        #X = reconstruct_list(flatten(text_features), offsets)
        #Y = reconstruct_list(        Y_labels      , offsets)
        #for a,b in zip(X,Y):
        #    for x,y in zip(a,b):
        #        print y
        #        #print filter(lambda t:t[0]=='word', x.keys())
        #        print x.keys()
        #        print
        #    print '\n\n\n'

        # Vectorize features
        dvect = DictVectorizer()
        X_feats = dvect.fit_transform( flatten(text_features) )

        # CRF needs reconstructed lists
        if self._crf_enabled:
            X_feats  = reconstruct_list( list(X_feats) , offsets)
            Y_labels = reconstruct_list(      Y_labels , offsets)
            lib = crf
        else:
            lib = sci

        if globals_cliner.verbosity > 0: print '\ttraining classifiers (pass one) ' + p_or_n

        #for i,X in enumerate(X_feats):
        #    for j,x in enumerate(X):
        #        print x, '\t', Y_labels[i][j]
        #    print
        #exit()

        # Train classifier
        clf  = lib.train(X_feats, Y_labels)

        return dvect,clf



    def __generic_first_predict(self, p_or_n, text_features, dvect, clf):
        '''
        ClinerModel::__generic_first_predict()

        Purpose: Train that works for both prose and nonprose

        @param p_or_n.        <string> either "prose" or "nonprose"
        @param text_features. <list-of-lists> of feature dictionaries
        @param dvect.         <DictVectorizer>
        @param clf.           scikit-learn classifier
        '''

        # If nothing to predict, skip actual prediction
        if len(text_features) == 0:
            print '\tnothing to predict (pass one) ' + p_or_n
            return []

        # Save list structure to reconstruct after vectorization
        offsets = save_list_structure(text_features)

        if globals_cliner.verbosity > 0: print '\tvectorizing features (pass one) ' + p_or_n

        # Vectorize features
        X_feats = dvect.transform( flatten(text_features) )

        if globals_cliner.verbosity > 0: print '\tpredicting    labels (pass one) ' + p_or_n

        # CRF requires reconstruct lists
        if self._crf_enabled:
            X_feats  = reconstruct_list(list(X_feats) , offsets)
            lib = crf
        else:
            lib = sci

        #for X in X_feats:
        #    for x in X:
        #        print x
        #    print
        #print '\n'

        # Predict IOB labels
        out = lib.predict(clf, X_feats)

        # Format labels from output
        predictions  = reconstruct_list(out, offsets)
        return predictions





def extract_concept_features(chunked_sentences, inds_list, feat_obj):
    ''' extract conept (2nd pass) features from textual data '''
    X = []
    for s,inds in zip(chunked_sentences, inds_list):
        X += feat_obj.concept_features(s, inds)
    return X




def first_pass_data_and_labels(notes):

    '''
    first_pass_data_and_labels()

    Purpose: Interface with notes object to get text data and labels

    @param notes. List of Note objects
    @return <tuple> whose elements are:
              0) list of tokenized sentences
              1) list of labels for tokenized sentences

    >>> import os
    >>> from notes.note import Note
    >>> base_dir = os.path.join(os.getenv('CLINER_DIR'), 'tests', 'data')
    >>> txt = os.path.join(base_dir, 'single.txt')
    >>> con = os.path.join(base_dir, 'single.con')
    >>> note_tmp = Note('i2b2')
    >>> note_tmp.read(txt, con)
    >>> notes = [note_tmp]
    >>> first_pass_data_and_labels(notes)
    ([['The', 'score', 'stood', 'four', 'to', 'two', ',', 'with', 'but', 'one', 'inning', 'more', 'to', 'play', ',']], [['B', 'I', 'I', 'I', 'I', 'I', 'O', 'O', 'O', 'O', 'O', 'O', 'O', 'O', 'O']])
    '''

    # Get the data and annotations from the Note objects
    l_tokenized_sentences = [ note.getTokenizedSentences() for note in notes ]
    l_iob_labels          = [ note.getIOBLabels()          for note in notes ]

    tokenized_sentences = flatten(l_tokenized_sentences)
    iob_labels          = flatten(l_iob_labels         )

    return tokenized_sentences, iob_labels




def second_pass_data_and_labels(notes):

    '''
    second_pass_data_and_labels()

    Purpose: Interface with notes object to get text data and labels

    @param notes. List of Note objects
    @return <tuple> whose elements are:
              0) list of chunked sentences
              0) list of list-of-indices designating chunks
              1) list of labels for chunks

    >>> import os
    >>> from notes.note import Note
    >>> base_dir = os.path.join(os.getenv('CLINER_DIR'), 'tests', 'data')
    >>> txt = os.path.join(base_dir, 'single.txt')
    >>> con = os.path.join(base_dir, 'single.con')
    >>> note_tmp = Note('i2b2')
    >>> note_tmp.read(txt, con)
    >>> notes = [note_tmp]
    >>> second_pass_data_and_labels(notes)
    ([['The score stood four to two', ',', 'with', 'but', 'one', 'inning', 'more', 'to', 'play', ',']], [[0]], ['problem'])
    '''

    # Get the data and annotations from the Note objects
    l_chunked_sentences  = [  note.getChunkedText()     for  note  in  notes  ]
    l_inds_list          = [  note.getConceptIndices()  for  note  in  notes  ]
    l_con_labels         = [  note.getConceptLabels()   for  note  in  notes  ]

    chunked_sentences = flatten(l_chunked_sentences)
    inds_list         = flatten(l_inds_list        )
    con_labels        = flatten(l_con_labels       )

    #print 'labels: ', len(con_labels)
    #print 'inds:   ', sum(map(len,inds_list))
    #exit()

    return chunked_sentences, inds_list, con_labels



def first_pass_data(note):

    '''
    first_pass_data()

    Purpose: Interface with notes object to get first pass data

    @param note. Note objects
    @return      <list> of tokenized sentences

    >>> import os
    >>> from notes.note import Note
    >>> base_dir = os.path.join(os.getenv('CLINER_DIR'), 'tests', 'data')
    >>> txt = os.path.join(base_dir, 'single.txt')
    >>> note = Note('i2b2')
    >>> note.read(txt)
    >>> first_pass_data(note)
    [['The', 'score', 'stood', 'four', 'to', 'two', ',', 'with', 'but', 'one', 'inning', 'more', 'to', 'play', ',']]
    '''

    return note.getTokenizedSentences()



def second_pass_data(note):

    '''
    second_pass_data()

    Purpose: Interface with notes object to get second pass data

    @param notes. List of Note objects
    @return <tuple> whose elements are:
              0) list of chunked sentences
              0) list of list-of-indices designating chunks

    >>> import os
    >>> from notes.note import Note
    >>> base_dir = os.path.join(os.getenv('CLINER_DIR'), 'tests', 'data')
    >>> txt = os.path.join(base_dir, 'single.txt')
    >>> note = Note('i2b2')
    >>> note.read(txt, con)
    >>> second_pass_data(note)
    ([['The score stood four to two', ',', 'with', 'but', 'one', 'inning', 'more', 'to', 'play', ',']], [[0]])
    '''

    # Get the data and annotations from the Note objects
    chunked_sentences = note.getChunkedText()
    inds              = note.getConceptIndices()

    return chunked_sentences, inds

def fit_bag_of_words(bow_obj, data, unlabeled_data=None):

    global bow

    """ needs to be a list of strings """

    corpus = data

    if unlabeled_data is not None:

        corpus += unlabeled_data

    if bow_obj.is_fitted() is False:

        docs = [' '.join(chunk) for chunk in corpus]

        bow_obj.fit(docs)


def concat(a,b):
    return a+b


