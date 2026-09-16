"""Immutable oracle for borealis-violet:code_relationships."""
import service


def main():
    assert service.relationship_target() == 'guarded_ledger_helper_violet', service.relationship_target()


if __name__ == "__main__":
    main()
