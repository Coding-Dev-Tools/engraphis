"""Immutable oracle for ember-west:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-ember-west', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
