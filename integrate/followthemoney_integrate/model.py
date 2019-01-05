import json
import logging
# from banal import ensure_list
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy import Column, MetaData, String, Integer, Float, DateTime
from sqlalchemy.sql.expression import func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import sessionmaker
from followthemoney import model
from followthemoney.util import get_entity_id

from followthemoney_integrate import settings
from followthemoney_integrate.util import index_text, text_parts

log = logging.getLogger(__name__)
now = datetime.utcnow
engine = create_engine(settings.DATABASE_URI)
metadata = MetaData(bind=engine)
Session = sessionmaker(bind=engine)
Base = declarative_base(bind=engine, metadata=metadata)


class Entity(Base):
    id = Column(String(255), primary_key=True)
    schema = Column(String(255))
    origin = Column(String(255))
    properties = Column(String)
    context = Column(String)
    search = Column(String)
    priority = Column(Float, nullable=True)
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)

    @declared_attr
    def __tablename__(cls):
        return settings.DATABASE_PREFIX + '_entity'

    def to_dict(self):
        data = json.loads(self.context)
        data['id'] = self.id
        data['schema'] = self.schema
        data['properties'] = json.loads(self.properties)
        return data

    @hybrid_property
    def proxy(self):
        if not hasattr(self, '_proxy'):
            self._proxy = model.get_proxy(self.to_dict())
        return self._proxy

    @proxy.setter
    def proxy(self, proxy):
        self._proxy = proxy
        self.id = proxy.id
        self.schema = proxy.schema.name
        self.properties = json.dumps(proxy.properties)
        self.context = json.dumps(proxy.context)

    @classmethod
    def save(cls, session, origin, proxy):
        obj = cls.by_id(session, proxy.id)
        if obj is None:
            obj = cls()
            obj.origin = origin
        obj.proxy = proxy
        obj.search = index_text(proxy)
        obj.updated_at = now()
        session.add(obj)
        return obj

    @classmethod
    def by_id(cls, session, entity_id):
        if entity_id is None:
            return
        q = session.query(cls)
        q = q.filter(cls.id == entity_id)
        return q.first()

    @classmethod
    def all(cls, session):
        q = session.query(cls)
        for entity in q.yield_per(10000):
            yield entity

    @classmethod
    def by_search(cls, session, query):
        q = session.query(cls)
        for text in text_parts(query):
            q = q.filter(cls.search.like('%%%s%%' % text))
        q = q.order_by(cls.priority.desc())
        q = q.order_by(cls.id.asc())
        return q

    @classmethod
    def by_priority(cls, session, user):
        # TODO: remove voted entities
        q = session.query(Match.subject)
        dq = session.query(Vote.match_id)
        dq = dq.filter(Vote.match_id == Match.id)
        dq = dq.filter(Vote.user == user)
        q = q.filter(~dq.exists())
        q = q.filter(Match.judgement == None)  # noqa
        q = q.group_by(Match.subject)
        q = q.order_by(func.avg(Match.score).desc())
        q = q.limit(1)
        for entity_id, in q.all():
            return entity_id


class Match(Base):
    id = Column(String(513), primary_key=True)
    subject = Column(String(255))
    candidate = Column(String(255))
    score = Column(Float, nullable=True)
    priority = Column(Float, nullable=True)
    judgement = Column(String(5), nullable=True)
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)

    @declared_attr
    def __tablename__(cls):
        return settings.DATABASE_PREFIX + '_match'

    @classmethod
    def save(cls, session, subject, candidate, score=None, judgement=None):
        obj = cls.by_id(session, subject, candidate)
        if obj is None:
            obj = cls()
            obj.id = cls.make_id(subject, candidate)
            obj.subject = get_entity_id(subject)
            obj.candidate = get_entity_id(candidate)
        if score is not None:
            obj.score = score
        if judgement is not None:
            obj.judgement = judgement
        obj.updated_at = now()
        session.add(obj)
        return obj

    @classmethod
    def update(cls, session, match_id, judgement):
        q = session.query(cls)
        q = q.filter(cls.id == match_id)
        q.update({'judgement': judgement})
        session.execute(q)

    @classmethod
    def tally(cls, session, updated=False):
        q = Vote.tally(session, updated=updated)
        for (match_id, judgement, count) in q:
            log.info("Decided: %s (%s w/ %d votes)",
                     match_id, judgement, count)
            cls.update(session, match_id, judgement)

    @classmethod
    def by_id(cls, session, subject, candidate):
        q = session.query(cls)
        q = q.filter(cls.id == cls.make_id(subject, candidate))
        return q.first()

    @classmethod
    def make_id(cls, subject, candidate):
        subject = get_entity_id(subject)
        candidate = get_entity_id(candidate)
        return '.'.join((subject, candidate))

    @classmethod
    def all(cls, session):
        q = session.query(cls)
        for match in q.yield_per(10000):
            yield match

    @classmethod
    def by_entity(cls, session, entity_id):
        q = session.query(Match, Entity)
        q = q.filter(Match.subject == entity_id)
        q = q.filter(Match.candidate == Entity.id)
        q = q.order_by(Match.score.desc())
        return q.limit(500)


class Vote(Base):
    id = Column(Integer, primary_key=True, autoincrement=True)
    match_id = Column(String(513), index=True)
    user = Column(String(255))
    judgement = Column(String(5))
    created_at = Column(DateTime, default=now)
    updated_at = Column(DateTime, default=now, onupdate=now)

    @declared_attr
    def __tablename__(cls):
        return settings.DATABASE_PREFIX + '_vote'

    @classmethod
    def save(cls, session, match_id, user, judgement):
        a, b = match_id.split('.', 1)
        for (subject, candidate) in ((a, b), (b, a)):
            q = session.query(cls)
            q = q.filter(cls.match_id == Match.make_id(subject, candidate))
            q = q.filter(cls.user == user)
            obj = q.first()
            if obj is None:
                obj = cls()
                obj.match_id = match_id
                obj.user = user
            obj.judgement = judgement
            obj.updated_at = now()
            session.add(obj)

    @classmethod
    def by_entity(cls, session, user, entity_id):
        q = session.query(Match.candidate, Vote.judgement)
        q = q.filter(Match.subject == entity_id)
        q = q.filter(Vote.match_id == Match.id)
        q = q.filter(Vote.user == user)
        return {e: j for (e, j) in q.all()}

    @classmethod
    def tally(cls, session, updated=False):
        count = func.count(Vote.id).label('count')
        dq = session.query(Vote.match_id.label('match_id'),
                           Vote.judgement.label('judgement'),
                           count)
        # dq = dq.filter(Vote.judgement != '?')
        if updated:
            dq = dq.filter(Match.id == Vote.match_id)
            dq = dq.filter(Match.updated_at <= Vote.updated_at)
        dq = dq.group_by(Vote.match_id, Vote.judgement)
        dq = dq.having(count >= settings.QUORUM)
        dq = dq.order_by(count.asc())
        return dq
