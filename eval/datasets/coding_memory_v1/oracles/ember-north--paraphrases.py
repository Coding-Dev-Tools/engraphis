"""Immutable oracle for ember-north:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-ember-north', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
